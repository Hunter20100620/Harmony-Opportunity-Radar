# -*- coding: utf-8 -*-
"""CLI 入口: python -m radar <capture|probe|audit|gui>

M4 版本化与熔断:
- 每次运行开启一个 `data/runs/<batch_id>/` 批次，产物与 LLM traces 归档其中。
- 单步重跑自动继承最近成功批次的上游产物。
- 任一阶段产出为空 / LLM 全失败时抛 PipelineAbortError，以非零退出码结束，
  且绝不覆写 data/ 顶层的有效历史产物。
"""
import argparse
import subprocess
import sys

from radar import store
from radar.config import load_config
from radar.pipeline import (
    EXIT_ABORT,
    EXIT_OK,
    EXIT_RETRYABLE,
    PipelineAbortError,
    RetryableAbortError,
)

# 命令 → (阶段名, 中文标签, 下一步提示)
COMMANDS = {
    "capture": ("capture", "榜单捕获", "下一步: python -m radar probe（检索鸿蒙同类供给与差评）"),
    "probe": ("probe", "供给探测", "下一步: python -m radar audit（四维打分审计）"),
    "audit": ("audit", "四维审计", "机会清单: data/opportunities.json"),
}


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="radar", description="纯血鸿蒙机会雷达——双端供给落差分析流水线"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_stage(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--batch-id", default=None,
                       help="复用指定批次目录（全量扫描时各阶段共享同一批次）")
        p.add_argument("--base-run-id", default=None,
                       help="指定继承上游产物的来源批次；默认取最近成功批次")
        p.add_argument("--emit-events", action="store_true",
                       help="向 stdout 输出结构化遥测事件行（供 WebUI 实时消费）")
        return p

    add_stage("capture", "Apple 标杆捕获（拉榜单 → LLM 精选 → JTBD）")
    add_stage("probe", "鸿蒙供给探测（M2）")
    add_stage("audit", "四维打分审计（M3）")
    sub.add_parser("gui", help="启动 Streamlit GUI 交互控制台")
    return parser


def _run_stage(stage: str, label: str, cfg: dict, batch_id: str | None,
               base_run_id: str | None, emit_events: bool = False) -> int:
    """在版本化批次中执行一个阶段，返回入选/产出数量。"""
    from radar import prompts
    batch = store.start_run(
        stage, batch_id=batch_id, base_run_id=base_run_id,
        cfg=cfg, prompts_snapshot=prompts.snapshot(), echo=emit_events,
    )
    print(f"[批次] {batch.batch_id}  ({label})")
    if batch.meta.get("inherited"):
        print(f"[继承] 来自 {batch.meta.get('base_run_id')}: "
              f"{', '.join(batch.meta['inherited'])}")

    try:
        if stage == "capture":
            from radar.pipeline import capture
            n = capture.run(cfg)
        elif stage == "probe":
            from radar.pipeline import probe
            n = probe.run(cfg)
        else:
            from radar.pipeline import audit
            n = audit.run(cfg)
    except RetryableAbortError as e:
        # 可重试中断：网络/LLM 临时故障，WebUI 会据此自动重试或等待继续
        batch.finish("interrupted", summary="可重试中断", error=str(e))
        print(f"\n[中断] {e}", file=sys.stderr)
        return -EXIT_RETRYABLE
    except PipelineAbortError as e:
        batch.finish("failed", summary="熔断中止", error=str(e))
        print(f"\n[熔断] {e}", file=sys.stderr)
        return -EXIT_ABORT
    except Exception as e:  # 其他异常同样标记批次失败，保留历史
        batch.finish("failed", summary="执行异常", error=f"{type(e).__name__}: {e}")
        print(f"\n[失败] {type(e).__name__}: {e}", file=sys.stderr)
        return -EXIT_ABORT

    summary = f"{label}完成: {n}"
    batch.finish("success", summary=summary)
    print(f"[批次] {batch.batch_id} 已归档（data/runs/{batch.batch_id}/）")
    return n


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "gui":
        print("启动 Streamlit GUI...")
        subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py"])
        return EXIT_OK

    cfg = load_config()
    stage, label, next_hint = COMMANDS[args.command]

    n = _run_stage(stage, label, cfg, args.batch_id, args.base_run_id,
                   emit_events=args.emit_events)
    if n < 0:
        # 负数编码失败性质：-1 致命熔断，-2 可重试中断 → 转成正退出码
        return abs(n)

    if stage == "capture":
        print(f"\n=== capture 完成: {n} 个标杆 ===\n{next_hint}")
    elif stage == "probe":
        print(f"\n=== probe 完成: {n} 个标杆 ===\n{next_hint}")
    else:
        print(f"\n=== audit 完成: {n} 个高潜机会入选 ===")
        print("机会清单: data/opportunities.json")
        print("决策看板: data/report.md")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())