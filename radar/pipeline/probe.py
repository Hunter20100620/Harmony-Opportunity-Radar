# -*- coding: utf-8 -*-
"""M2 鸿蒙供给探测流水线：关键词检索 → 规则+LLM 去噪 → 精选竞品评论。

输入：data/benchmarks.json（M1 产物）
输出：data/probe.json（每个标杆的鸿蒙侧供给事实）

流程：
  1. 遍历每个标杆，取前 2 个关键词搜索华为市场
  2. 包名去重 + 规则硬过滤（排除游戏/金融）
  3. 保留候选 Top 6~8 → LLM 语义相关度判定
  4. 仅对被 LLM 认可的 0~3 个真实竞品拉取评论
  5. 无真实竞品时标记 blank_signal=true（生态空白）
"""
import json
import time

from radar import store, telemetry
from radar.config import load_config
from radar.llm import LLMClient, LLMError
from radar.pipeline import PipelineAbortError
from radar.prompts import PROBE_FILTER_SYSTEM, PROBE_FILTER_USER
from radar.sources.appgallery import AppGalleryClient, AppGalleryError

STAGE = "probe"


def run(cfg: dict | None = None) -> int:
    if cfg is None:
        cfg = load_config()

    benchmarks = store.load_benchmarks()
    if not benchmarks:
        raise PipelineAbortError("benchmarks.json 为空或不存在，请先运行 capture")

    probe_cfg = cfg.get("probe", {})
    max_comments = probe_cfg.get("max_comments_per_app", 50)
    bad_threshold = probe_cfg.get("bad_rating_threshold", 3)
    filter_limit = probe_cfg.get("filter_candidates_limit", 8)
    enable_llm_filter = probe_cfg.get("enable_llm_filter", True)

    benchmarks_list = benchmarks.get("benchmarks", [])
    total = len(benchmarks_list)
    print(f"[M2 probe] 开始探测 {total} 个标杆的鸿蒙竞品…")
    print(f"          配置: LLM过滤={'开启' if enable_llm_filter else '关闭'}, "
          f"候选上限={filter_limit}, 差评阈值≤{bad_threshold}, 评论上限={max_comments}/app")
    telemetry.current() and telemetry.current().stage_start(STAGE, total)

    llm = LLMClient(cfg["llm"], stage=STAGE) if enable_llm_filter else None
    results = []
    client = AppGalleryClient()

    try:
        for idx, bm in enumerate(benchmarks_list, 1):
            name = bm.get("name", "?")
            keywords = bm.get("search_keywords", [])[:2]
            print(f"\n[{idx}/{total}] {name}")
            print(f"          关键词: {keywords}")
            telemetry.progress(STAGE, idx - 1, current=name)

            try:
                record = _probe_one_benchmark(
                    client, llm, bm, keywords,
                    max_comments, bad_threshold, filter_limit,
                )
                results.append(record)
                status = f"{len(record['competitors'])} 个竞品"
                if record.get("blank_signal"):
                    status += " [空白信号: 鸿蒙无真正同类]"
                print(f"          → {status}")
                telemetry.progress(STAGE, idx, current=name, message=status)
            except (AppGalleryError, RuntimeError) as e:
                print(f"          [warn] 探测失败: {e}")
                results.append({
                    "name": name,
                    "apple_id": bm.get("apple_id", ""),
                    "keywords_used": list(keywords),
                    "competitor_count": 0,
                    "competitors": [],
                    "blank_signal": True,
                    "error": str(e),
                })
                telemetry.progress(STAGE, idx, current=name, message=f"失败: {e}")
    finally:
        client.close()

    # ---- 熔断保护：探测结果为空则绝不覆写历史 ----
    if not results:
        raise PipelineAbortError(
            "probe 产出为空，触发熔断：已保留历史 data/probe.json 不被覆盖。"
        )

    store.save_probe({
        "probed_at": store._now(),
        "model": cfg.get("llm", {}).get("model", ""),
        "count": len(results),
        "benchmarks": results,
    })

    success = sum(1 for r in results if r["competitor_count"] > 0)
    blank = sum(1 for r in results if r.get("blank_signal"))
    print(f"\n=== probe 完成: {success}/{total} 有竞品, {blank} 空白信号 ===")
    print(f"结果已保存到 probe.json")
    print(f"下一步: python -m radar audit（四维打分审计）")
    if telemetry.current():
        telemetry.current().stage_done(
            STAGE, f"{success}/{total} 有竞品, {blank} 空白信号")
    return len(results)


def _safe_rating(val) -> float:
    if val is None:
        return 99
    try:
        return float(val)
    except (ValueError, TypeError):
        return 99


def _download_rank(dls_str: str) -> int:
    """将下载量描述字符串转为整数排名权重。"""
    dls = (dls_str or "").replace(",", "")
    if "亿" in dls:
        return int(float(dls.replace("亿次安装", "").replace("亿", "").replace("<", "").strip() or "0") * 100000000)
    if "万" in dls:
        return int(float(dls.replace("万次安装", "").replace("万", "").replace("<", "").strip() or "0") * 10000)
    try:
        return int(dls.replace("次安装", "").replace("<", "").strip() or "0")
    except ValueError:
        return 0


def _probe_one_benchmark(
    client: AppGalleryClient,
    llm: LLMClient | None,
    bm: dict,
    keywords: list[str],
    max_comments: int,
    bad_threshold: int,
    filter_limit: int,
) -> dict:
    """探测单个标杆的鸿蒙市场竞品。"""
    name = bm.get("name", "")
    apple_id = bm.get("apple_id", "")
    jtbd = bm.get("jtbd", "")

    # 第 1 步：关键词检索 + 包名去重（规则过滤已在 search 内完成）
    seen_pkg: dict[str, dict] = {}
    for kw in keywords:
        if not kw:
            continue
        apps = client.search(kw, max_results=25)
        for app in apps:
            pkg = app.get("package", "")
            if not pkg or pkg in seen_pkg:
                continue
            seen_pkg[pkg] = app

    if not seen_pkg:
        return _blank_record(name, apple_id, keywords, jtbd)

    # 第 2 步：按下载量/评分排序，截取候选池
    sorted_candidates = sorted(
        seen_pkg.values(),
        key=lambda a: (float(a.get("score", 0) or 0), _download_rank(a.get("downloads", ""))),
        reverse=True,
    )
    candidates = sorted_candidates[:filter_limit]

    # 第 3 步：LLM 语义相关度判定
    if llm:
        try:
            approved_packages = _llm_filter_competitors(llm, name, jtbd, candidates)
        except LLMError as e:
            print(f"          [warn] LLM 过滤失败({e})，回退到规则排序")
            approved_packages = None
    else:
        approved_packages = None

    # approved_packages=None 表示 LLM 不可用/回退，使用规则排序的前 3 个
    if approved_packages is not None:
        top = [a for a in candidates if a.get("package", "") in approved_packages]
        if not top:
            # LLM 明确判定无同类 → 生态空白
            return {**_blank_record(name, apple_id, keywords, jtbd),
                    "llm_verdict": "LLM判定无同等同类竞品"}
        blank_signal_from_llm = False
    else:
        top = candidates[:3]
        blank_signal_from_llm = False

    # 第 4 步：对被认可的竞品抓取详情和评论
    competitors = []
    for app in top:
        appid = app.get("appid", app.get("package", ""))
        if not appid:
            continue
        time.sleep(0.3)

        detail = None
        try:
            detail = client.detail(appid)
        except AppGalleryError:
            pass

        bad_reviews = []
        all_reviews = []
        try:
            cmt = client.comments(appid, page_num=1, page_size=25)
            all_reviews = cmt.get("list", [])
            if len(all_reviews) >= 25 and len(all_reviews) < max_comments:
                cmt2 = client.comments(appid, page_num=2, page_size=25)
                all_reviews.extend(cmt2.get("list", []))
            bad_reviews = [
                r for r in all_reviews
                if r.get("rating", 0) is not None and _safe_rating(r["rating"]) <= bad_threshold
            ][:max_comments]
        except AppGalleryError:
            pass

        competitor = {
            "name": app.get("name", ""),
            "appid": appid,
            "package": app.get("package", ""),
            "score": app.get("score", 0),
            "downloads": app.get("downloads", ""),
            "category": app.get("category", ""),
            "version": detail.get("version", app.get("version", "")) if detail else app.get("version", ""),
            "update_time": detail.get("update_time", "") if detail else "",
            "intro": detail.get("intro", app.get("intro", "")) if detail else app.get("intro", ""),
            "comment_count": len(all_reviews),
            "bad_review_count": len(bad_reviews),
            "bad_reviews": bad_reviews,
        }
        competitors.append(competitor)

    has_meaningful = any(
        c.get("name", "") and float(c.get("score", 0) or 0) > 0
        for c in competitors
    )

    return {
        "name": name,
        "apple_id": apple_id,
        "keywords_used": list(keywords),
        "competitor_count": len(competitors),
        "competitors": competitors,
        "blank_signal": blank_signal_from_llm or not has_meaningful,
    }


def _llm_filter_competitors(
    llm: LLMClient,
    benchmark_name: str,
    benchmark_jtbd: str,
    candidates: list[dict],
) -> list[str]:
    """调用 LLM 从候选应用中筛选出真正的同类竞品。"""
    # 精简候选信息，控制上下文
    slim = [
        {"name": a.get("name", ""), "category": a.get("category", ""),
         "package": a.get("package", ""), "intro": a.get("intro", "")[:80]}
        for a in candidates
    ]
    candidates_json = json.dumps(slim, ensure_ascii=False, indent=2)
    prompt = PROBE_FILTER_USER.format(
        benchmark_name=benchmark_name,
        benchmark_jtbd=benchmark_jtbd,
        candidates_json=candidates_json,
    )
    result = llm.chat_json(PROBE_FILTER_SYSTEM, prompt, temperature=0.1,
                           stage=STAGE, target=benchmark_name)
    if isinstance(result, list):
        return [str(pkg) for pkg in result]
    if isinstance(result, dict):
        # 部分模型可能套一层对象
        for v in result.values():
            if isinstance(v, list):
                return [str(pkg) for pkg in v]
    print(f"          [warn] LLM 返回格式异常({type(result).__name__})，回退")
    return []


def _blank_record(name: str, apple_id: str, keywords: list[str],
                  jtbd: str = "") -> dict:
    return {
        "name": name,
        "apple_id": apple_id,
        "keywords_used": list(keywords),
        "competitor_count": 0,
        "competitors": [],
        "blank_signal": True,
    }