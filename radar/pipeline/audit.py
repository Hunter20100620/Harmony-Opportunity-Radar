# -*- coding: utf-8 -*-
"""M3 四维机会审计流水线：事实配对 → LLM 审计 → 看板渲染。

输入：data/benchmarks.json（M1）、data/probe.json（M2）
输出：data/opportunities.json（结构化机会清单）
      data/report.md（Markdown 决策看板）

流程：
  1. 按 apple_id 将 benchmarks 与 probe 配对
  2. 构建审计上下文（竞品概况 + 差评原文）
  3. 逐个调用 LLM 进行四维打分与原生赋能评估
  4. 校验/计算综合得分，按 min_score 过滤
  5. 原子写入 opportunities.json
  6. 渲染 Markdown 决策看板并写入 report.md

熔断保护（M4）:
  - 若所有 LLM 调用均失败（LLMError），或成功解析但入选机会为 0，
    则抛出 PipelineAbortError，**绝不**写入空产物覆盖有效历史数据。
"""
import json

from radar import store, telemetry
from radar.config import load_config
from radar.llm import LLMClient, LLMError
from radar.pipeline import PipelineAbortError
from radar.prompts import AUDIT_SYSTEM, AUDIT_USER

STAGE = "audit"


def run(cfg: dict | None = None) -> int:
    if cfg is None:
        cfg = load_config()

    benchmarks_data = _load_or_fail("benchmarks")
    probe_data = _load_or_fail("probe")

    benchmarks_list = benchmarks_data.get("benchmarks", [])
    if not benchmarks_list:
        raise PipelineAbortError("benchmarks.json 中 benchmarks 列表为空，审计中止")
    probe_idx = _build_probe_index(probe_data)

    audit_cfg = cfg.get("audit", {})
    min_score = float(audit_cfg.get("min_score", 6.0))

    llm = LLMClient(cfg["llm"], stage=STAGE)

    opportunities = []
    idx_map = _build_benchmark_index(benchmarks_list)

    total = len(benchmarks_list)
    print(f"[M3 audit] 开始对 {total} 个标杆进行四维机会审计…")
    print(f"          最低得分门槛: {min_score}, 模型: {cfg['llm']['model']}")
    telemetry.current() and telemetry.current().stage_start(STAGE, total)

    failed = 0          # LLM 调用失败计数
    invalid = 0         # 返回格式异常计数
    for i, bm in enumerate(benchmarks_list, 1):
        name = bm.get("name", "?")
        apple_id = bm.get("apple_id", "")

        probe_record = _match_probe(probe_idx, bm, idx_map)

        print(f"\n[{i}/{total}] {name}")
        telemetry.progress(STAGE, i - 1, current=name)
        context = _build_audit_context(bm, probe_record)
        print(f"          竞品: {context['competitor_count']} 个"
              f"{' [生态空白]' if context['is_blank'] else ''}")

        try:
            result = llm.chat_json(AUDIT_SYSTEM, AUDIT_USER.format(**context),
                                   temperature=0.1, stage=STAGE, target=name)
        except LLMError as e:
            print(f"          [warn] LLM 审计失败: {e}")
            failed += 1
            telemetry.progress(STAGE, i, current=name, message=f"失败: {e}")
            continue

        audit_item = _normalize_audit_result(bm, probe_record, result)
        if audit_item is None:
            print(f"          [warn] 审计结果格式异常，跳过")
            invalid += 1
            telemetry.progress(STAGE, i, current=name, message="结果格式异常")
            continue

        overall = audit_item["scores"]["overall"]
        passed = overall >= min_score
        tag = "✅ 入选" if passed else "⏭ 未达标"
        print(f"          综合得分: {overall:.1f} {tag}")
        telemetry.progress(STAGE, i, current=name, message=f"{overall:.1f} {tag}")

        opportunities.append(audit_item)

    # ---- 熔断保护：绝不用空/无效结果覆写历史 ----
    if not opportunities:
        if failed == total:
            reason = f"全部 {total} 个标杆的 LLM 审计调用均失败"
        elif failed + invalid == total:
            reason = f"全部 {total} 个标杆均未产出有效审计结果（失败 {failed} / 格式异常 {invalid}）"
        else:
            reason = f"审计产出为空（失败 {failed} / 格式异常 {invalid} / 总数 {total}）"
        raise PipelineAbortError(
            f"{reason}，触发熔断：已保留历史 data/opportunities.json 不被覆盖。"
            f"请检查 LLM 服务（{cfg['llm']['base_url']}）后重试。"
        )

    # 按综合得分降序排列
    opportunities.sort(key=lambda x: x["scores"]["overall"], reverse=True)

    opp_data = {
        "audited_at": store._now(),
        "model": cfg.get("llm", {}).get("model", ""),
        "min_score": min_score,
        "total_audited": len(opportunities),
        "opportunities": opportunities,
    }
    store.save_opportunities(opp_data)
    print(f"\n=== audit 完成: {len(opportunities)} 个高潜机会保存到 opportunities.json ===")

    # 渲染决策看板
    markdown = _render_markdown_report(opp_data, cfg)
    store.save_report(markdown)
    print(f"决策看板已生成: report.md")

    if telemetry.current():
        telemetry.current().stage_done(
            STAGE, f"{len(opportunities)} 个高潜机会（失败 {failed} / 异常 {invalid}）")
    return len(opportunities)


def _load_or_fail(stage: str) -> dict:
    data = store.load_json(f"{stage}.json")
    if not data:
        raise PipelineAbortError(f"{stage}.json 不存在或为空，请先运行前序阶段")
    return data


def _build_probe_index(probe_data: dict) -> dict[str, dict]:
    """以 name 为键建立 probe 索引。"""
    idx = {}
    for rec in probe_data.get("benchmarks", []):
        n = rec.get("name", "")
        if n:
            idx[n] = rec
    return idx


def _build_benchmark_index(benchmarks_list: list) -> dict[str, dict]:
    idx = {}
    for bm in benchmarks_list:
        aid = bm.get("apple_id", "")
        n = bm.get("name", "")
        if aid:
            idx[aid] = bm
        if n:
            idx[n] = bm
    return idx


def _match_probe(probe_idx: dict, bm: dict, bm_idx: dict) -> dict | None:
    name = bm.get("name", "")
    apple_id = bm.get("apple_id", "")
    for key in (name, apple_id):
        if key and key in probe_idx:
            return probe_idx[key]
    return None


def _build_audit_context(bm: dict, probe_record: dict | None) -> dict:
    """构建审计输入上下文。（注意：此函数名内含下划线，用户调用时勿漏。）"""
    is_blank = True
    competitor_count = 0
    competitor_details = "（无竞品，鸿蒙生态空白）"
    recent_bad_reviews = "--- 差评痛点 ---\n（无差评数据）"

    if probe_record:
        is_blank = probe_record.get("blank_signal", False)
        competitors = probe_record.get("competitors", [])
        competitor_count = len(competitors)

        if competitors:
            lines = []
            for c in competitors:
                bad_n = len(c.get("bad_reviews", []))
                lines.append(
                    f"  - {c.get('name', '?')} | 评分: {c.get('score', '?')} "
                    f"| 下载: {c.get('downloads', '?')} "
                    f"| 差评: {bad_n}条"
                )
            competitor_details = "\n".join(lines) if lines else "（竞品信息缺失）"

            pain_parts = []
            for c in competitors:
                bad = c.get("bad_reviews", [])
                if not bad:
                    continue
                name_c = c.get("name", "?")
                pain_parts.append(f"  [{name_c} 差评]:")
                for r in bad[:5]:
                    content = r.get("content", "").strip()
                    rating = r.get("rating", "?")
                    if content:
                        pain_parts.append(f"    - [{rating}星] {content[:120]}")
            if pain_parts:
                recent_bad_reviews = "--- 差评痛点 ---\n" + "\n".join(pain_parts)

    return {
        "benchmark_name": bm.get("name", ""),
        "artist": bm.get("artist", ""),
        "jtbd": bm.get("jtbd", ""),
        "pick_reason": bm.get("pick_reason", ""),
        "is_blank": "是" if is_blank else "否",
        "competitor_count": str(competitor_count),
        "competitor_details": competitor_details,
        "recent_bad_reviews": recent_bad_reviews,
    }


def _normalize_audit_result(bm: dict, probe_record: dict | None, raw: object) -> dict | None:
    """校验 LLM 返回并填充 fallback 字段。返回标准化的审计条目，或 None（格式不合法）。"""
    if not isinstance(raw, dict):
        return None

    scores_raw = raw.get("scores")
    if not isinstance(scores_raw, dict):
        return None

    try:
        demand = float(scores_raw.get("demand", 0))
        gap = float(scores_raw.get("experience_gap", 0))
        native = float(scores_raw.get("native_advantage", 0))
        feasibility = float(scores_raw.get("indie_feasibility", 0))
    except (ValueError, TypeError):
        return None

    computed = demand * 0.25 + gap * 0.30 + native * 0.25 + feasibility * 0.20
    overall_raw = scores_raw.get("overall")
    try:
        overall = float(overall_raw) if overall_raw is not None else computed
    except (ValueError, TypeError):
        overall = computed

    verdict = raw.get("verdict", "")
    native_features = raw.get("native_features", [])
    attack_vector = raw.get("attack_vector", "")
    indie_advice = raw.get("indie_advice", "")

    competitor_list = []
    is_blank = False
    if probe_record:
        is_blank = probe_record.get("blank_signal", False)
        for c in probe_record.get("competitors", []):
            bad_refined = _summarize_pain(c.get("bad_reviews", []))
            competitor_list.append({
                "name": c.get("name", ""),
                "score": c.get("score", ""),
                "downloads": c.get("downloads", ""),
                "category": c.get("category", ""),
                "bad_review_count": c.get("bad_review_count", 0),
                "pain_summary": bad_refined,
            })

    return {
        "benchmark_name": bm.get("name", ""),
        "apple_id": bm.get("apple_id", ""),
        "artist": bm.get("artist", ""),
        "jtbd": bm.get("jtbd", ""),
        "pick_reason": bm.get("pick_reason", ""),
        "is_blank": is_blank,
        "competitors": competitor_list,
        "scores": {
            "demand": round(demand, 1),
            "experience_gap": round(gap, 1),
            "native_advantage": round(native, 1),
            "indie_feasibility": round(feasibility, 1),
            "overall": round(overall, 1),
        },
        "verdict": verdict,
        "native_features": native_features if isinstance(native_features, list) else [],
        "attack_vector": attack_vector,
        "indie_advice": indie_advice,
    }


def _summarize_pain(bad_reviews: list) -> list:
    """从原始差评中提取精简痛点。"""
    pains = []
    for r in bad_reviews[:5]:
        content = r.get("content", "").strip()
        rating = r.get("rating", "?")
        if content:
            pains.append(f"[{rating}星] {content[:100]}")
    return pains


def _render_markdown_report(opp_data: dict, cfg: dict) -> str:
    """用纯 Python 渲染结构化 Markdown 决策看板。"""
    ops = opp_data.get("opportunities", [])
    total = opp_data.get("total_audited", len(ops))
    min_score = opp_data.get("min_score", 6.0)
    now = opp_data.get("audited_at", "")

    blank_count = sum(1 for o in ops if o.get("is_blank"))
    high_count = sum(1 for o in ops if o["scores"]["overall"] >= 8.0)

    lines = []
    _a = lines.append

    _a(f"# 纯血鸿蒙机会雷达 · 决策看板")
    _a(f"")
    _a(f"> 生成时间: {now}  |  审计模型: {opp_data.get('model', '?')}  |  最低门槛: {min_score}")
    _a(f"")
    _a(f"---")
    _a(f"")
    _a(f"## 看板总览")
    _a(f"")
    _a(f"| 指标 | 数值 |")
    _a(f"| :--- | :--- |")
    _a(f"| 审计标杆总数 | {total} |")
    _a(f"| 入选高潜机会 | {len(ops)} |")
    _a(f"| 其中生态空白项目 | {blank_count} |")
    _a(f"| 高优先级（≥8.0 分） | {high_count} |")
    _a(f"")

    # ---- 机会天梯榜 ----
    _a(f"---")
    _a(f"## 机会天梯榜")
    _a(f"")
    _a(f"| # | 应用 | 综合分 | 需求 | 落差 | 原生 | 可行性 | 定性 |")
    _a(f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for rank, o in enumerate(ops, 1):
        s = o["scores"]
        blank_tag = " 🟦[空白]" if o.get("is_blank") else ""
        name_display = o["benchmark_name"]
        _a(f"| {rank} | {name_display}{blank_tag} "
           f"| {s['overall']:.1f} | {s['demand']:.1f} | {s['experience_gap']:.1f} "
           f"| {s['native_advantage']:.1f} | {s['indie_feasibility']:.1f} "
           f"| {o.get('verdict', '')[:30]} |")
    _a(f"")

    # ---- 深度机会卡片 ----
    _a(f"---")
    _a(f"## 深度机会卡片")
    _a(f"")
    for rank, o in enumerate(ops, 1):
        s = o["scores"]
        blank_tag = " 🟦 生态空白" if o.get("is_blank") else ""
        _a(f"### {rank}. {o['benchmark_name']} — {s['overall']:.1f} 分{blank_tag}")
        _a(f"")
        _a(f"**iOS 标杆**: {o['benchmark_name']} by {o['artist']}")
        _a(f"- JTBD: *{o['jtbd']}*")
        _a(f"- 入选理由: {o['pick_reason']}")
        _a(f"")
        _a("| 维度 | 得分 | 解读 |")
        _a("| :--- | :--- | :--- |")
        _a(f"| 需求验证 | {s['demand']:.1f} | 场景普遍性与刚需度 |")
        _a(f"| 体验落差 | {s['experience_gap']:.1f} | 鸿蒙供给空白/竞品差评空间 |")
        _a(f"| 原生契合 | {s['native_advantage']:.1f} | 实况窗/卡片/小艺/端侧AI |")
        _a(f"| 开发可行 | {s['indie_feasibility']:.1f} | 单人端侧成本 |")
        _a(f"| **综合** | **{s['overall']:.1f}** | **{o.get('verdict', '')}** |")
        _a(f"")

        if o.get("competitors"):
            _a("**鸿蒙市场竞品现状**:")
            for c in o["competitors"]:
                _a(f"- {c['name']} (评分 {c['score']}, 下载 {c['downloads']})")
                if c.get("pain_summary"):
                    for p in c["pain_summary"]:
                        _a(f"  - 差评: {p}")
            _a("")
        else:
            _a("**鸿蒙市场现状**: 无真实同类竞品（生态空白）")
            _a("")

        _a(f"**鸿蒙原生赋能建议**:")
        for feat in o.get("native_features", []):
            _a(f"- {feat}")
        _a(f"")
        _a(f"**MVP 突破切入点**: {o.get('attack_vector', '')}")
        _a(f"")
        _a(f"**独立开发者建议**: {o.get('indie_advice', '')}")
        _a(f"")
        _a(f"---")
        _a(f"")

    # ---- 生态空白抢跑区 ----
    blanks = [o for o in ops if o.get("is_blank")]
    if blanks:
        _a(f"## 🟦 生态空白抢跑区")
        _a(f"")
        _a(f"以下项目鸿蒙市场尚无真正同类竞品，优先度极高：")
        _a(f"")
        _a(f"| # | 应用 | 综合分 | 核心看点 |")
        _a(f"| :--- | :--- | :--- | :--- |")
        for rank, o in enumerate(blanks, 1):
            s = o["scores"]
            _a(f"| {rank} | {o['benchmark_name']} | {s['overall']:.1f} "
               f"| {o.get('attack_vector', '')[:50]} |")
        _a(f"")

    _a(f"---")
    _a(f"*报告由 Harmony Opportunity Radar 自动生成*")

    return "\n".join(lines)