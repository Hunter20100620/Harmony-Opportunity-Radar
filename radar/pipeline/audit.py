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
import math

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

    all_audits: list[dict] = []
    passed_opportunities: list[dict] = []
    rejected_audits: list[dict] = []
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
        eligible = _is_main_eligible(audit_item)
        passed = overall >= min_score and eligible
        if passed:
            tag = "✅ 入选"
        elif overall >= min_score:
            tag = "⏭ 未达标（证据不足，转人工复核）"
        else:
            tag = "⏭ 未达标"
        print(f"          综合得分: {overall:.1f} {tag}")
        telemetry.progress(STAGE, i, current=name, message=f"{overall:.1f} {tag}")

        all_audits.append(audit_item)
        if passed:
            passed_opportunities.append(audit_item)
        else:
            rejected_audits.append(audit_item)

    # 完整审计产物：无论是否达标都保留全部有效结果，便于人工复核
    audit_results = {
        "audited_at": store._now(),
        "model": cfg.get("llm", {}).get("model", ""),
        "min_score": min_score,
        "total_audited": len(all_audits),
        "total_passed": len(passed_opportunities),
        "total_rejected": len(rejected_audits),
        "failed": failed,
        "invalid": invalid,
        "audits": sorted(all_audits, key=lambda x: x["scores"]["overall"], reverse=True),
    }
    store.save_audit_results(audit_results)

    # 人工复核队列：在熔断前生成，保证失败批次里低证据项仍可追溯
    review_queue = _build_review_queue(all_audits, probe_data.get("benchmarks", []),
                                       min_score)
    store.save_review_queue({
        "generated_at": store._now(),
        "count": len(review_queue),
        "items": review_queue,
    })

    # 批次质量统计写入 meta（不含任何密钥）
    batch = store.active_batch()
    if batch is not None:
        batch.meta["quality"] = store.summarize_quality(
            probe_data.get("benchmarks", []), all_audits, failed, invalid)
        batch._write_meta()

    # ---- 熔断保护：绝不用空/无效结果覆写历史 ----
    if not passed_opportunities:
        if not all_audits:
            if failed == total:
                reason = f"全部 {total} 个标杆的 LLM 审计调用均失败"
            elif failed + invalid == total:
                reason = f"全部 {total} 个标杆均未产出有效审计结果（失败 {failed} / 格式异常 {invalid}）"
            else:
                reason = f"审计产出为空（失败 {failed} / 格式异常 {invalid} / 总数 {total}）"
        else:
            reason = (f"{len(all_audits)} 个有效审计结果全部低于门槛 {min_score}"
                      f"（失败 {failed} / 格式异常 {invalid}）")
        raise PipelineAbortError(
            f"{reason}，触发熔断：已保留历史 data/opportunities.json 不被覆盖。"
            f"请检查 LLM 服务（{cfg['llm']['base_url']}）后重试。"
        )

    # 按综合得分降序排列
    passed_opportunities.sort(key=lambda x: x["scores"]["overall"], reverse=True)

    opp_data = {
        "audited_at": store._now(),
        "model": cfg.get("llm", {}).get("model", ""),
        "min_score": min_score,
        "total_audited": len(all_audits),
        "total_passed": len(passed_opportunities),
        "total_rejected": len(rejected_audits),
        "opportunities": passed_opportunities,
    }
    store.save_opportunities(opp_data)
    print(f"\n=== audit 完成: {len(passed_opportunities)} 个高潜机会保存到 opportunities.json ===")

    # 渲染决策看板
    markdown = _render_markdown_report(opp_data, cfg)
    store.save_report(markdown)
    print(f"决策看板已生成: report.md")

    if telemetry.current():
        telemetry.current().stage_done(
            STAGE, f"{len(passed_opportunities)} 个高潜机会（失败 {failed} / 异常 {invalid}）")
    return len(passed_opportunities)


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


# 证据不足或请求失败的状态：不得在提示词中标记为「生态空白」
_NON_BLANK_STATUSES = ("request_error", "llm_filter_failed",
                       "weak_evidence", "no_search_results", "partial")

# 这些状态的审计结果即使分数达标，也不得进入主机会结果（仅保留在完整审计与复核队列）
_INELIGIBLE_STATUSES = _NON_BLANK_STATUSES


def _is_main_eligible(audit_item: dict) -> bool:
    """判断审计结果是否有资格进入主机会结果（opportunities）。

    证据不足 / 请求失败 / LLM 过滤失败 / 无检索结果的条目即使分数达标，
    也只能进入待复核池，不得污染主机会结果。
    """
    status = audit_item.get("probe_status", "")
    return status not in _INELIGIBLE_STATUSES


def _build_audit_context(bm: dict, probe_record: dict | None) -> dict:
    """构建审计输入上下文。（注意：此函数名内含下划线，用户调用时勿漏。）"""
    is_blank = True
    competitor_count = 0
    competitor_details = "（无竞品，鸿蒙生态空白）"
    recent_bad_reviews = "--- 差评痛点 ---\n（无差评数据）"

    probe_status = "unknown"
    evidence_grade = ""
    query_stats = {}
    search_errors: list = []
    blank_confidence = ""

    if probe_record:
        is_blank = probe_record.get("blank_signal", False)
        probe_status = probe_record.get("probe_status", "unknown")
        evidence_grade = probe_record.get("evidence_grade", "")
        query_stats = probe_record.get("query_stats", {}) or {}
        search_errors = probe_record.get("search_errors", []) or []
        blank_confidence = probe_record.get("blank_confidence", "")
        # 请求失败 / LLM 过滤失败 / 证据不足：绝不标记为生态空白
        if probe_status in _NON_BLANK_STATUSES:
            is_blank = False
        competitors = probe_record.get("competitors", [])
        competitor_count = len(competitors)

        if competitors:
            lines = []
            for c in competitors:
                bad_n = len(c.get("bad_reviews", []))
                cs = c.get("comment_stats", {}) or {}
                evidence = c.get("review_evidence", "missing")
                lines.append(
                    f"  - {c.get('name', '?')} | 评分: {c.get('score', '?')} "
                    f"| 下载: {c.get('downloads', '?')} "
                    f"| 差评: {bad_n}条 "
                    f"| 评论样本: {cs.get('fetched', 0)}条(证据:{evidence})"
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

    if probe_status in _NON_BLANK_STATUSES:
        is_blank_text = "否（证据不足）"
    else:
        is_blank_text = "是" if is_blank else "否"

    return {
        "benchmark_name": bm.get("name", ""),
        "artist": bm.get("artist", ""),
        "jtbd": bm.get("jtbd", ""),
        "pick_reason": bm.get("pick_reason", ""),
        "is_blank": is_blank_text,
        "competitor_count": str(competitor_count),
        "competitor_details": competitor_details,
        "recent_bad_reviews": recent_bad_reviews,
        "probe_status": probe_status,
        "evidence_grade": evidence_grade,
        "query_stats": json.dumps(query_stats, ensure_ascii=False),
        "search_errors": json.dumps(search_errors, ensure_ascii=False),
        "blank_confidence": blank_confidence,
    }


_REQUIRED_DIMENSIONS = ("demand", "experience_gap",
                        "native_advantage", "indie_feasibility")


def _parse_dimensions(scores: dict) -> tuple[float, float, float, float] | None:
    """解析并校验四个必需维度，任一非法即返回 None。

    拒绝布尔值、非数字、非有限值（NaN/Inf）以及超出 [0, 10] 的值。
    """
    values = []
    for key in _REQUIRED_DIMENSIONS:
        val = scores.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        v = float(val)
        if not math.isfinite(v) or not (0.0 <= v <= 10.0):
            return None
        values.append(v)
    return tuple(values)  # type: ignore[return-value]


# 证据等级 → experience_gap 保守上限（None 表示不额外封顶）
_GAP_CAPS = {"D": 4.0, "C": 7.0, "B": None, "A": None}


def _apply_evidence_caps(audit_item: dict, probe_record: dict | None) -> dict:
    """依据证据等级对 experience_gap 施加代码级保守上限，并重算 overall。

    上限只在真正降低分数时记录 score_cap_applied 与原因。
    """
    if not probe_record:
        return audit_item
    grade = probe_record.get("evidence_grade")
    cap = _GAP_CAPS.get(grade)
    if cap is None:
        return audit_item

    scores = audit_item.get("scores", {})
    gap = float(scores.get("experience_gap", 0) or 0)
    if gap <= cap:
        return audit_item

    scores["experience_gap"] = round(cap, 1)
    scores["overall"] = _compute_overall(scores)
    audit_item["scores"] = scores
    audit_item["score_cap_applied"] = True
    audit_item["score_cap_reason"] = (
        f"证据等级 {grade}，experience_gap 由 {gap:.1f} 降至 {cap:.1f}")
    return audit_item


def _compute_overall(scores: dict) -> float:
    """按固定权重计算综合得分，四舍五入到一位小数。不读取模型自报 overall。"""
    demand = float(scores.get("demand", 0))
    gap = float(scores.get("experience_gap", 0))
    native = float(scores.get("native_advantage", 0))
    feasibility = float(scores.get("indie_feasibility", 0))
    return round(demand * 0.25 + gap * 0.30 + native * 0.25 + feasibility * 0.20, 1)


def _normalize_audit_result(bm: dict, probe_record: dict | None, raw: object) -> dict | None:
    """校验 LLM 返回并填充 fallback 字段。返回标准化的审计条目，或 None（格式不合法）。"""
    if not isinstance(raw, dict):
        return None

    scores_raw = raw.get("scores")
    if not isinstance(scores_raw, dict):
        return None

    dimensions = _parse_dimensions(scores_raw)
    if dimensions is None:
        return None
    demand, gap, native, feasibility = dimensions

    # overall 永远由代码按固定权重计算，模型自报值只作诊断
    computed = _compute_overall({
        "demand": demand,
        "experience_gap": gap,
        "native_advantage": native,
        "indie_feasibility": feasibility,
    })
    overall = computed

    model_overall = None
    overall_consistent = None
    model_overall_raw = scores_raw.get("overall")
    if model_overall_raw is not None:
        try:
            model_overall = float(model_overall_raw)
        except (ValueError, TypeError):
            model_overall = None
    if model_overall is not None:
        overall_consistent = (round(model_overall, 1) == round(computed, 1))

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

    result = {
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
        "model_overall": model_overall,
        "overall_consistent": overall_consistent,
        "probe_status": probe_record.get("probe_status", "") if probe_record else "",
        "evidence_grade": probe_record.get("evidence_grade", "") if probe_record else "",
    }
    result = _apply_evidence_caps(result, probe_record)
    return result


def _summarize_pain(bad_reviews: list) -> list:
    """从原始差评中提取精简痛点。"""
    pains = []
    for r in bad_reviews[:5]:
        content = r.get("content", "").strip()
        rating = r.get("rating", "?")
        if content:
            pains.append(f"[{rating}星] {content[:100]}")
    return pains


_REVIEW_STATUSES = ("request_error", "llm_filter_failed",
                    "weak_evidence", "no_search_results", "partial")


def _build_review_queue(audits: list[dict], probes: list[dict],
                        min_score: float = 6.0) -> list[dict]:
    """生成人工复核队列：请求错误、LLM 过滤失败、低证据、低 JTBD 置信度与临界项。

    确认 A 级且达标的机会默认不入队。
    """
    probe_by_id = {}
    for p in probes or []:
        if p.get("apple_id"):
            probe_by_id[p["apple_id"]] = p
        if p.get("name"):
            probe_by_id.setdefault(p["name"], p)

    queue = []
    for item in audits:
        reasons = []
        apple_id = item.get("apple_id", "")
        name = item.get("benchmark_name", "")
        pr = probe_by_id.get(apple_id) or probe_by_id.get(name) or {}
        status = pr.get("probe_status", item.get("probe_status", ""))
        grade = pr.get("evidence_grade", item.get("evidence_grade", ""))
        overall = float(item.get("scores", {}).get("overall", 0) or 0)

        if status in _REVIEW_STATUSES:
            reasons.append(status)
        if grade in ("C", "D"):
            reasons.append(f"evidence_grade_{grade}")
        if overall >= min_score and not _is_main_eligible(item):
            reasons.append("pending_review")
        if abs(overall - min_score) <= 0.5:
            reasons.append("near_threshold")

        if reasons:
            queue.append({
                "benchmark_name": name,
                "apple_id": apple_id,
                "reason_codes": reasons,
                "evidence_grade": grade,
                "probe_status": status,
                "review_questions": ["是否需要补充关键词或人工确认？"],
            })
    return queue


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