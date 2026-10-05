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
    max_keywords = probe_cfg.get("max_keywords_per_benchmark", 4)
    max_competitors = probe_cfg.get("max_competitors", 3)
    comment_pages = probe_cfg.get("comment_pages", 4)

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
            keywords, skipped = _select_keywords(
                bm.get("search_keywords", []), max_keywords)
            print(f"\n[{idx}/{total}] {name}")
            print(f"          关键词: {keywords}"
                  + (f"（跳过 {len(skipped)} 个）" if skipped else ""))
            telemetry.progress(STAGE, idx - 1, current=name)

            try:
                record = _probe_one_benchmark(
                    client, llm, bm, keywords,
                    max_comments, bad_threshold, filter_limit,
                    max_competitors=max_competitors,
                    comment_pages=comment_pages,
                )
                record["keywords_available"] = len(bm.get("search_keywords", []))
                record["keywords_skipped"] = skipped
                results.append(record)
                status = f"{len(record['competitors'])} 个竞品"
                if record.get("blank_signal"):
                    status += " [空白信号: 鸿蒙无真正同类]"
                print(f"          → {status}")
                telemetry.progress(STAGE, idx, current=name, message=status)
            except (AppGalleryError, RuntimeError) as e:
                print(f"          [warn] 探测失败: {e}")
                results.append(_make_record(
                    name, bm.get("apple_id", ""), list(keywords),
                    probe_status="request_error", blank_signal=False,
                    search_errors=[{"keyword": "", "error": str(e)}]))
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


# probe 状态机：区分「确认有竞品 / 确认空白 / 无搜索结果 / 请求失败 /
# LLM 过滤失败 / 证据薄弱 / 部分成功」。只有 confirmed_empty 才是真正的生态空白。
PROBE_STATUSES = (
    "confirmed_competitors",
    "confirmed_empty",
    "no_search_results",
    "request_error",
    "llm_filter_failed",
    "weak_evidence",
    "partial",
)


def _empty_query_stats() -> dict:
    return {"requested": 0, "succeeded": 0, "failed": 0, "non_empty": 0}


def _derive_evidence_grade(record: dict) -> str:
    """仅依据结构化探测事实推导证据等级 A/B/C/D。

    不读取任何 LLM 分数，避免用打分结果反推证据质量。
      A: 多关键词成功 + 有竞品且评论证据充分
      B: 检索成功 + 有竞品但评论/详情不完整
      C: 检索成功但无确认竞品或覆盖有限
      D: 请求错误 / LLM 过滤失败 / 证据不可用
    """
    status = record.get("probe_status", "")
    stats = record.get("query_stats", {}) or {}
    competitors = record.get("competitors", []) or []

    if status in ("request_error", "llm_filter_failed"):
        return "D"
    if status in ("no_search_results", "confirmed_empty"):
        return "C"

    if not competitors:
        return "C"

    succeeded = int(stats.get("succeeded", 0) or 0)
    requested = int(stats.get("requested", 0) or 0)
    multi_keyword = requested > 1 and succeeded == requested
    sufficient_reviews = all(
        (c.get("review_evidence") in ("sufficient",)) for c in competitors)

    if multi_keyword and sufficient_reviews:
        return "A"
    return "B"


def _make_record(name: str, apple_id: str, keywords: list[str], *,
                 probe_status: str, blank_signal: bool = False,
                 competitors: list | None = None,
                 query_stats: dict | None = None,
                 search_errors: list | None = None,
                 **extra) -> dict:
    """构造统一的 probe 记录，保证状态字段齐全。"""
    record = {
        "name": name,
        "apple_id": apple_id,
        "keywords_used": list(keywords),
        "probe_status": probe_status,
        "competitor_count": len(competitors or []),
        "competitors": competitors or [],
        "blank_signal": blank_signal,
        "query_stats": query_stats or _empty_query_stats(),
        "search_errors": search_errors or [],
    }
    record.update(extra)
    record["evidence_grade"] = _derive_evidence_grade(record)
    if blank_signal:
        record["blank_confidence"] = "high"
    else:
        record["blank_confidence"] = "none"
    return record


def _select_keywords(raw_keywords: list, limit: int) -> tuple[list[str], list[str]]:
    """去重非空关键词并保留顺序，返回（选用, 跳过）两个列表。"""
    seen: set[str] = set()
    normalized: list[str] = []
    for kw in raw_keywords or []:
        kw = str(kw).strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        normalized.append(kw)
    return normalized[:limit], normalized[limit:]


def _probe_one_benchmark(
    client: AppGalleryClient,
    llm: LLMClient | None,
    bm: dict,
    keywords: list[str],
    max_comments: int,
    bad_threshold: int,
    filter_limit: int,
    max_competitors: int = 3,
    comment_pages: int = 4,
) -> dict:
    """探测单个标杆的鸿蒙市场竞品，返回带明确状态的可追溯记录。"""
    name = bm.get("name", "")
    apple_id = bm.get("apple_id", "")
    jtbd = bm.get("jtbd", "")

    # 第 1 步：关键词检索 + 包名去重（规则过滤已在 search 内完成）
    query_stats = _empty_query_stats()
    search_errors: list[dict] = []
    seen_pkg: dict[str, dict] = {}
    for kw in keywords:
        if not kw:
            continue
        query_stats["requested"] += 1
        try:
            apps = client.search(kw, max_results=25)
        except AppGalleryError as e:
            query_stats["failed"] += 1
            search_errors.append({"keyword": kw, "error": str(e)})
            continue
        query_stats["succeeded"] += 1
        if apps:
            query_stats["non_empty"] += 1
        for app in apps:
            pkg = app.get("package", "")
            if not pkg or pkg in seen_pkg:
                continue
            seen_pkg[pkg] = app

    # 全部请求失败 → 请求错误，绝非生态空白
    if query_stats["succeeded"] == 0:
        return _make_record(
            name, apple_id, keywords, probe_status="request_error",
            blank_signal=False, query_stats=query_stats,
            search_errors=search_errors)

    # 检索成功但无任何候选 → 无搜索结果，需人工确认后才可能为空白
    if not seen_pkg:
        return _make_record(
            name, apple_id, keywords, probe_status="no_search_results",
            blank_signal=False, query_stats=query_stats,
            search_errors=search_errors)

    # 第 2 步：按下载量/评分排序，截取候选池
    sorted_candidates = sorted(
        seen_pkg.values(),
        key=lambda a: (float(a.get("score", 0) or 0), _download_rank(a.get("downloads", ""))),
        reverse=True,
    )
    candidates = sorted_candidates[:filter_limit]

    # 第 3 步：LLM 语义相关度判定（返回结构化状态）
    filter_status = "disabled"
    approved_packages: list[str] = []
    filter_error = None
    if llm:
        filt = _llm_filter_competitors(llm, name, jtbd, candidates,
                                       max_competitors=max_competitors)
        filter_status = filt.get("status", "invalid_response")
        approved_packages = filt.get("packages", []) or []
        filter_error = filt.get("error")

    if filter_status == "confirmed_empty":
        return _make_record(
            name, apple_id, keywords, probe_status="confirmed_empty",
            blank_signal=True, query_stats=query_stats,
            search_errors=search_errors,
            candidate_pool_size=len(seen_pkg), candidates_considered=len(candidates),
            llm_verdict="LLM判定无同等同类竞品", llm_filter_status=filter_status)

    if filter_status in ("invalid_response", "request_error"):
        return _make_record(
            name, apple_id, keywords, probe_status="llm_filter_failed",
            blank_signal=False, query_stats=query_stats,
            search_errors=search_errors,
            candidate_pool_size=len(seen_pkg), candidates_considered=len(candidates),
            unverified_candidates=[
                {"name": a.get("name", ""), "package": a.get("package", "")}
                for a in candidates
            ],
            filter_error=filter_error, llm_filter_status=filter_status)

    # confirmed（LLM 认可）或 disabled（LLM 关闭，回退规则排序）
    if filter_status == "confirmed":
        top = [a for a in candidates if a.get("package", "") in approved_packages]
    else:
        top = candidates[:3]
    # 代码层硬上限：最终竞品数绝不超过配置值
    top = top[:max_competitors]

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

        all_reviews, comment_status = _collect_comments(
            client, appid, comment_pages, max_comments)
        comment_stats = _comment_stats(all_reviews)
        bad_reviews = [
            r for r in all_reviews
            if r.get("rating", 0) is not None and _safe_rating(r["rating"]) <= bad_threshold
        ][:max_comments]

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
            "comment_stats": comment_stats,
            "review_evidence": comment_status,
            "rating_distribution": comment_stats["rating_distribution"],
        }
        competitors.append(competitor)

    has_meaningful = any(
        c.get("name", "") and float(c.get("score", 0) or 0) > 0
        for c in competitors
    )

    status = "confirmed_competitors" if has_meaningful else "weak_evidence"
    return _make_record(
        name, apple_id, keywords, probe_status=status,
        blank_signal=False, competitors=competitors,
        query_stats=query_stats, search_errors=search_errors,
        candidate_pool_size=len(seen_pkg), candidates_considered=len(candidates),
        llm_filter_status=filter_status)


def _collect_comments(client, appid: str, comment_pages: int,
                      max_comments: int) -> tuple[list[dict], str]:
    """分页抓取评论，返回（扁平评论列表, review_evidence 状态）。

    review_evidence 取值：sufficient / insufficient / request_error / missing。
    评论请求失败与「零差评」是两回事，必须区分。
    """
    all_reviews: list[dict] = []
    request_failed = False
    for page in range(1, max(1, comment_pages) + 1):
        try:
            cmt = client.comments(appid, page_num=page, page_size=25)
        except AppGalleryError:
            request_failed = True
            break
        page_list = cmt.get("list", []) or []
        if not page_list:
            break
        all_reviews.extend(page_list)
        if len(all_reviews) >= max_comments:
            break
    all_reviews = all_reviews[:max_comments]

    if request_failed:
        return all_reviews, "request_error"
    if not all_reviews:
        return all_reviews, "missing"
    valid = sum(1 for r in all_reviews
                if r.get("rating") is not None and _safe_rating(r["rating"]) != 99)
    if valid >= 3:
        return all_reviews, "sufficient"
    return all_reviews, "insufficient"


def _comment_stats(reviews: list[dict]) -> dict:
    """计算评论样本统计：抓取量、有效评分、差评数与评分分布。"""
    valid = 0
    bad = 0
    dist: dict[str, int] = {}
    for r in reviews:
        rating = r.get("rating")
        if rating is None or _safe_rating(rating) == 99:
            continue
        valid += 1
        v = int(_safe_rating(rating))
        if v <= 3:
            bad += 1
        key = str(v)
        dist[key] = dist.get(key, 0) + 1
    return {
        "fetched": len(reviews),
        "valid_rating": valid,
        "bad_rating": bad,
        "bad_ratio": round(bad / valid, 3) if valid else 0.0,
        "rating_distribution": dist,
    }


def _llm_filter_competitors(
    llm: LLMClient,
    benchmark_name: str,
    benchmark_jtbd: str,
    candidates: list[dict],
    max_competitors: int = 3,
) -> dict:
    """调用 LLM 从候选应用中筛选出真正的同类竞品，返回结构化状态。

    返回 {"status", "packages", "error"}，status 取值：
      confirmed        有确认的同类竞品
      confirmed_empty  LLM 明确返回合法空数组（无同类）
      invalid_response 格式异常或返回了列表外的包名
      request_error    LLM 调用失败
    """
    candidate_packages = {a.get("package", "") for a in candidates}
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
        max_competitors=max_competitors,
    )
    try:
        result = llm.chat_json(PROBE_FILTER_SYSTEM, prompt, temperature=0.1,
                               stage=STAGE, target=benchmark_name)
    except LLMError as e:
        return {"status": "request_error", "packages": [], "error": str(e)}

    raw_list = None
    if isinstance(result, list):
        raw_list = result
    elif isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list):
                raw_list = v
                break

    # 结构非法（非数组、缺数组）→ 格式失败，绝不能当作确认空白
    if raw_list is None:
        return {"status": "invalid_response", "packages": [],
                "error": f"LLM 返回非数组结构: {type(result).__name__}"}

    # 合法空数组 → 确认无同类
    if len(raw_list) == 0:
        return {"status": "confirmed_empty", "packages": [], "error": None}

    # 白名单：只接受候选列表内原样出现的包名
    valid = [str(p) for p in raw_list if str(p) in candidate_packages]
    if not valid:
        return {"status": "invalid_response", "packages": [],
                "error": "LLM 返回的包名均不在候选列表内"}

    return {"status": "confirmed", "packages": valid, "error": None}