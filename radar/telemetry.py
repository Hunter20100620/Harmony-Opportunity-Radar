# -*- coding: utf-8 -*-
"""遥测事件总线：流水线 → 前端（Streamlit 子进程）的结构化进度与调用追踪通道。

设计约束（SPEC 轻量化）:
- 只依赖标准库。不引入 MQ / 数据库 / 监控框架。
- 子进程把事件同时写入:
    1. stdout 结构化行  `__RADAR_EVENT__:{json}`  —— 供父进程实时消费（WebUI 进度条）
    2. 当前批次的 `progress.jsonl`                —— 供落盘回溯/断线补读
- 所有事件在进程内单例累积，`emit()` 永不抛错（遥测失败绝不能拖垮流水线）。

阶段（stage）取值: capture / probe / audit。
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

# stdout 结构化事件前缀：父进程按此识别，普通日志行不受影响
EVENT_PREFIX = "__RADAR_EVENT__:"

# 进度百分比锚点：每个阶段在总进度中的起止区间（capture→probe→audit 顺序）
STAGE_RANGES = {
    "capture": (2.0, 20.0),
    "probe": (20.0, 62.0),
    "audit": (62.0, 100.0),
}
STAGE_LABELS = {
    "capture": "榜单捕获",
    "probe": "供给探测",
    "audit": "四维审计",
}

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Telemetry:
    """单进程遥测收集器：持有当前批次目录，广播 progress / trace 事件。"""

    def __init__(self, run_dir=None, echo: bool = True, batch_id: str | None = None):
        # run_dir: 当前批次目录（None 表示仅输出 stdout，不落盘）
        self.run_dir = str(run_dir) if run_dir else None
        self.echo = echo
        self.batch_id = batch_id
        self._events: list[dict] = []
        self._progress_path = (
            os.path.join(self.run_dir, "progress.jsonl") if self.run_dir else None
        )
        # 每阶段动态条目数（标杆数），用于把「第 i/n 个」映射到全局百分比
        self._stage_total: dict[str, int] = {}

    # ---- 事件发送 ----
    def emit(self, event: dict) -> None:
        """广播一个事件（永不抛错）。"""
        try:
            event = dict(event)
            event.setdefault("ts", _now())
            if self.batch_id:
                event.setdefault("batch_id", self.batch_id)
            with _lock:
                self._events.append(event)
                if self._progress_path:
                    self._append_line(self._progress_path, event)
            if self.echo:
                # 单行输出，父进程按前缀解析；json 内无换行
                sys.stdout.write(EVENT_PREFIX + json.dumps(event, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception:
            # 遥测是旁路：任何异常都吞掉，绝不中断主流程
            pass

    def log(self, stage: str, message: str, level: str = "info") -> None:
        self.emit({"type": "log", "stage": stage, "level": level, "message": message})

    def stage_start(self, stage: str, total: int, label: str = "") -> None:
        self._stage_total[stage] = max(int(total), 1)
        self.emit({
            "type": "stage", "stage": stage,
            "label": label or STAGE_LABELS.get(stage, stage),
            "total": int(total), "status": "start",
        })
        self.progress(stage, 0, current="")

    def progress(self, stage: str, index: int, current: str = "", pct: float | None = None,
                 message: str = "") -> None:
        """上报阶段内第 index 个条目的进度。pct 显式给出时不按区间换算。"""
        total = self._stage_total.get(stage, 1)
        if pct is None:
            pct = self._stage_pct(stage, index, total)
        self.emit({
            "type": "progress", "stage": stage,
            "label": STAGE_LABELS.get(stage, stage),
            "index": int(index), "total": total,
            "pct": round(float(pct), 1), "current": current, "message": message,
        })

    def stage_done(self, stage: str, summary: str = "") -> None:
        pct = STAGE_RANGES.get(stage, (0.0, 100.0))[1]
        self.emit({
            "type": "stage", "stage": stage,
            "label": STAGE_LABELS.get(stage, stage), "status": "done",
            "pct": pct, "message": summary,
        })

    def trace(self, trace: dict) -> None:
        self.emit({"type": "trace", **trace})

    def run_done(self, status: str, message: str = "") -> None:
        self.emit({"type": "run", "status": status, "pct": 100.0, "message": message})

    # ---- 内部 ----
    @staticmethod
    def _stage_pct(stage: str, index: int, total: int) -> float:
        lo, hi = STAGE_RANGES.get(stage, (0.0, 100.0))
        frac = 0.0 if total <= 0 else max(0.0, min(1.0, index / total))
        return lo + (hi - lo) * frac

    @staticmethod
    def _append_line(path: str, event: dict) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ---- 进程内单例：pipeline 与 llm 共享同一实例 ----
_INSTANCE: Telemetry | None = None


def configure(run_dir=None, echo: bool = True, batch_id: str | None = None) -> Telemetry:
    """初始化/重设当前进程遥测实例（每次 CLI 运行开始时调用一次）。"""
    global _INSTANCE
    _INSTANCE = Telemetry(run_dir=run_dir, echo=echo, batch_id=batch_id)
    return _INSTANCE


def current() -> Telemetry | None:
    return _INSTANCE


def emit(event: dict) -> None:
    if _INSTANCE is not None:
        _INSTANCE.emit(event)


def progress(stage: str, index: int, current: str = "", pct: float | None = None,
             message: str = "") -> None:
    if _INSTANCE is not None:
        _INSTANCE.progress(stage, index, current=current, pct=pct, message=message)


def log(stage: str, message: str, level: str = "info") -> None:
    if _INSTANCE is not None:
        _INSTANCE.log(stage, message, level=level)


def trace(trace_dict: dict) -> None:
    if _INSTANCE is not None:
        _INSTANCE.trace(trace_dict)


def parse_event_line(line: str) -> dict | None:
    """从一行 stdout 文本解析遥测事件；非事件行返回 None。"""
    line = (line or "").lstrip("\ufeff").strip()
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        return json.loads(line[len(EVENT_PREFIX):])
    except (ValueError, json.JSONDecodeError):
        return None