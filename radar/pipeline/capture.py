# -*- coding: utf-8 -*-
"""M1 标杆捕获流水线: RSS 拉取 → LLM 精选+JTBD 抽象 → 落盘。

产物（都在 data/ 下）:
  raw_feed.json    原始榜单全量快照（LLM 处理前，便于复盘/重跑）
  benchmarks.json  精选后的标杆列表（M2 probe 的输入）
"""
import json
from datetime import datetime, timezone

from radar import store, telemetry
from radar.llm import LLMClient, LLMError
from radar.pipeline import PipelineAbortError
from radar.prompts import CAPTURE_SYSTEM, CAPTURE_USER
from radar.sources import apple_rss

STAGE = "capture"


def run(cfg: dict) -> int:
    """执行捕获流水线，返回标杆数量（用于 CLI 汇总输出）。"""
    cap_cfg = cfg["capture"]

    # ---- 第 1 步: 拉取 Apple 榜单原始数据 ----
    print("[1/3] 拉取 Apple RSS 榜单 "
          f"({','.join(cap_cfg['regions'])} × {','.join(cap_cfg['charts'])}) ...")
    telemetry.current() and telemetry.current().stage_start(STAGE, 1, "榜单捕获")
    telemetry.progress(STAGE, 0, message="拉取 Apple RSS 榜单…")
    apps = apple_rss.fetch_all(
        regions=cap_cfg["regions"],
        charts=cap_cfg["charts"],
        limit=cap_cfg["feed_limit"],
    )
    if not apps:
        raise PipelineAbortError("所有榜单均拉取失败，检查网络后重试")

    # 原始快照先落盘——即使后续 LLM 失败，也不用重新拉网络
    store.save_raw_feed({"captured_at": _now(), "count": len(apps), "apps": apps})
    print(f"      共 {len(apps)} 个应用（去重后），已存 raw_feed.json")
    telemetry.progress(STAGE, 0, message=f"已拉取 {len(apps)} 个应用")

    # ---- 第 2 步: LLM 精选 + JTBD 抽象 ----
    print(f"[2/3] LLM 精选中（模型: {cfg['llm']['model']}）...")
    telemetry.progress(STAGE, 0, message="LLM 精选中…")
    llm = LLMClient(cfg["llm"], stage=STAGE, target="榜单精选")
    # 只喂 LLM 需要的精简字段，减小上下文；最多 200 个以免推理模型超时
    slim_apps = [
        {"name": a["name"], "artist": a["artist"], "apple_id": a["apple_id"],
         "rank": a["rank"]}
        for a in apps
    ][:200]
    prompt_user = CAPTURE_USER.format(
        apps_json=json.dumps(slim_apps, ensure_ascii=False),
        max_benchmarks=cap_cfg["max_benchmarks"],
    )
    try:
        picks = llm.chat_json(CAPTURE_SYSTEM, prompt_user, stage=STAGE, target="榜单精选")
    except LLMError as e:
        # LLM 挂了不让整条流水线白跑：原始数据已在盘上，给用户明确指引
        raise PipelineAbortError(
            f"LLM 精选失败: {e}\n"
            f"原始数据已保存在 raw_feed.json，"
            f"修好 LLM 服务（{cfg['llm']['base_url']}）后重跑 capture 即可，无需重新拉取。"
        ) from e

    if not isinstance(picks, list):
        raise PipelineAbortError(f"LLM 返回了非数组结构: {type(picks)}")
    if not picks:
        raise PipelineAbortError("LLM 精选结果为空，触发熔断：保留历史 benchmarks.json 不被覆盖。")

    # ---- 第 3 步: 回填原始榜单元数据（rank/url/source）并落盘 ----
    # LLM 输出只有 name/artist，用原始数据补全 url、来源榜单等事实字段
    by_name = {a["name"].lower(): a for a in apps}
    benchmarks = []
    for p in picks:
        origin = by_name.get(str(p.get("name", "")).lower(), {})
        benchmarks.append({
            "name": p.get("name", ""),
            "artist": p.get("artist", ""),
            "apple_id": p.get("apple_id") or origin.get("apple_id", ""),
            "url": origin.get("url", ""),
            "rank": origin.get("rank"),
            "source_charts": origin.get("source", ""),  # 该标杆在哪些榜单出现
            "jtbd": p.get("jtbd", ""),
            "search_keywords": p.get("search_keywords", []),
            "pick_reason": p.get("pick_reason", ""),
        })

    store.save_benchmarks({
        "captured_at": _now(),
        "model": cfg["llm"]["model"],
        "count": len(benchmarks),
        "benchmarks": benchmarks,
    })
    print(f"[3/3] 完成！精选 {len(benchmarks)} 个标杆，已存 benchmarks.json")
    if telemetry.current():
        telemetry.current().stage_done(STAGE, f"精选 {len(benchmarks)} 个标杆")
    return len(benchmarks)


def _now() -> str:
    """当前时间（UTC+8），所有数据快照统一用这个时间戳字段。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")