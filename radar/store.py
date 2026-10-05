# -*- coding: utf-8 -*-
"""数据存取: 统一管理 data/ 目录下的 JSON 文件（数据总线）。

M4 版本化改造:
- 产物按批次写入 `data/runs/<batch_id>/`（batch_id 形如 20260925_120000）。
- 单步重跑（probe/audit）自动从最近一次成功批次继承上游产物
  （benchmarks.json / probe.json），无需重跑前序阶段。
- 批次成功时，把产物镜像同步回 `data/` 顶层传统路径，
  保证既有 CLI 工具与第三方调用继续可用（向下兼容）。
- 所有写入仍是「临时文件 + os.replace」原子替换。

无活跃批次时（例如直接调用 store.save_json 的旧代码），行为与旧版完全一致：
直接写 `data/` 顶层。
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

from radar.config import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"
LATEST_POINTER = DATA_DIR / "latest.json"

# 每个阶段「产出」的产物；其余为继承的上游输入
STAGE_OUTPUTS = {
    "capture": ["raw_feed.json", "benchmarks.json"],
    "probe": ["probe.json"],
    "audit": ["audit_results.json", "opportunities.json", "review_queue.json",
              "report.md"],
}

# 单步重跑需要从上游批次继承的产物
STAGE_INPUTS = {
    "capture": [],
    "probe": ["benchmarks.json"],
    "audit": ["benchmarks.json", "probe.json"],
}

# 批次内所有可能的产物文件
ALL_ARTIFACTS = ["raw_feed.json", "benchmarks.json", "probe.json",
                 "audit_results.json", "opportunities.json", "review_queue.json",
                 "report.md"]

# 当前活跃批次（None 表示直接写顶层，兼容旧行为）
_active_batch = None


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    """当前时间（UTC+8），用于各阶段产物时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_text(path, text: str) -> str:
    """将文本原子写入 path（同目录临时文件 + os.replace）。"""
    target = str(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def _atomic_write_json(path, data) -> str:
    return _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ============================================================
# 批次生命周期管理
# ============================================================
class RunBatch:
    """一次流水线运行的版本化批次：目录、元数据、产物读写与镜像同步。"""

    def __init__(self, batch_id: str, run_dir, meta: dict):
        self.batch_id = batch_id
        self.dir = run_dir
        self.meta = meta

    # ---- 路径 ----
    def path(self, filename: str) -> str:
        return str(self.dir / filename)

    def exists(self, filename: str) -> bool:
        return (self.dir / filename).exists()

    # ---- 读写 ----
    def save_json(self, filename: str, data) -> str:
        return _atomic_write_json(self.dir / filename, data)

    def save_report(self, markdown: str) -> str:
        return _atomic_write_text(self.dir / "report.md", markdown)

    def load_json(self, filename: str, default=None):
        path = self.dir / filename
        if not path.exists():
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def append_trace(self, trace: dict) -> None:
        """追加一条 LLM 调用追踪到 llm_traces.jsonl。"""
        path = self.dir / "llm_traces.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(trace, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- 状态 ----
    def _write_meta(self) -> None:
        _atomic_write_json(self.dir / "meta.json", self.meta)

    def finish(self, status: str, summary: str = "", error: str | None = None) -> None:
        """结束批次：更新 meta；成功时镜像产物到顶层并更新 latest 指针。"""
        self.meta["status"] = status
        self.meta["ended_at"] = _now()
        self.meta["summary"] = summary
        if error:
            self.meta["error"] = error
        started = self.meta.get("started_at", "")
        self.meta["duration_s"] = _duration_s(started, self.meta["ended_at"])
        self._write_meta()

        if status == "success":
            _mirror_to_top_level(self)
            _update_latest_pointer(self.batch_id)


def _duration_s(started: str, ended: str) -> float:
    try:
        s = datetime.fromisoformat(started)
        e = datetime.fromisoformat(ended)
        return round((e - s).total_seconds(), 2)
    except (ValueError, TypeError):
        return 0.0


def _mirror_to_top_level(batch: RunBatch) -> None:
    """把批次内产物原子同步到 data/ 顶层传统路径（向下兼容）。"""
    _ensure_dir()
    for name in ALL_ARTIFACTS:
        src = batch.dir / name
        if not src.exists():
            continue
        try:
            text = src.read_text(encoding="utf-8")
            _atomic_write_text(DATA_DIR / name, text)
        except OSError:
            pass


def _update_latest_pointer(batch_id: str) -> None:
    try:
        _atomic_write_json(LATEST_POINTER, {
            "batch_id": batch_id,
            "updated_at": _now(),
        })
    except OSError:
        pass


def latest_run_id() -> str | None:
    """返回最近一次成功批次的 batch_id（读取 latest 指针，回退到目录扫描）。"""
    if LATEST_POINTER.exists():
        try:
            with open(LATEST_POINTER, encoding="utf-8") as f:
                bid = (json.load(f) or {}).get("batch_id")
            if bid and (RUNS_DIR / bid).exists():
                return bid
        except (OSError, ValueError):
            pass
    runs = list_runs(status="success")
    return runs[0]["batch_id"] if runs else None


def list_runs(status: str | None = None) -> list[dict]:
    """列出历史批次（按时间倒序）。每项含 batch_id / meta / mtime。"""
    if not RUNS_DIR.exists():
        return []
    items = []
    for entry in RUNS_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta = {}
        meta_path = entry / "meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f) or {}
            except (OSError, ValueError):
                meta = {}
        if status and meta.get("status") != status:
            continue
        items.append({"batch_id": entry.name, "meta": meta})
    items.sort(key=lambda x: x["batch_id"], reverse=True)
    return items


def run_dir(batch_id: str):
    return RUNS_DIR / batch_id


def start_run(stage: str, batch_id: str | None = None,
              base_run_id: str | None = None,
              cfg: dict | None = None,
              prompts_snapshot: dict | None = None,
              echo: bool = True) -> RunBatch:
    """开启一个新批次。

    - stage: capture / probe / audit / full
    - batch_id: 不传则用当前时间戳
    - base_run_id: 单步重跑时继承上游产物的来源批次；不传则自动取最近成功批次
    - 同时初始化进程内遥测实例，使其把 progress.jsonl 写入本批次目录
    """
    global _active_batch

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    explicit = bool(batch_id)
    if not batch_id:
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    run_path = RUNS_DIR / batch_id
    # 自动生成的时间戳撞名时加序号后缀，绝不覆盖既有批次；
    # 显式传入 batch_id（全量扫描共享同一批次）时按原样复用目录。
    if run_path.exists() and not explicit:
        suffix = 1
        while (RUNS_DIR / f"{batch_id}_{suffix}").exists():
            suffix += 1
        batch_id = f"{batch_id}_{suffix}"
        run_path = RUNS_DIR / batch_id
    run_path.mkdir(parents=True, exist_ok=True)

    # 读取既有 meta（同一批次被全量扫描多阶段复用时保留历史阶段信息）
    prev_meta = {}
    meta_path = run_path / "meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                prev_meta = json.load(f) or {}
        except (OSError, ValueError):
            prev_meta = {}
    stages = list(prev_meta.get("stages", []))
    if stage not in stages:
        stages.append(stage)

    # 单步重跑：继承上游产物（同一批次内已存在的上游产物则跳过）
    inherited: list[str] = []
    needed = [n for n in STAGE_INPUTS.get(stage, []) if not (run_path / n).exists()]
    base = base_run_id
    if base is None and needed:
        base = latest_run_id()
    if base and base != batch_id:
        base_path = RUNS_DIR / base
        for name in needed:
            src = base_path / name
            if src.exists():
                shutil.copy2(src, run_path / name)
                inherited.append(name)

    meta = {
        "batch_id": batch_id,
        "stage": stage,
        "stages": stages,
        "status": "running",
        "started_at": _now(),
        "base_run_id": base if base != batch_id else None,
        "inherited": inherited,
        "config": _slim_config(cfg),
        "model": (cfg or {}).get("llm", {}).get("model", ""),
    }
    batch = RunBatch(batch_id, run_path, meta)
    batch._write_meta()

    if prompts_snapshot is not None:
        batch.save_json("prompts_snapshot.json", prompts_snapshot)

    # 初始化遥测（写入本批次 progress.jsonl）
    from radar import telemetry
    telemetry.configure(run_dir=run_path, echo=echo, batch_id=batch_id)

    _active_batch = batch
    return batch


def active_batch() -> RunBatch | None:
    return _active_batch


def clear_active() -> None:
    global _active_batch
    _active_batch = None


def summarize_quality(probe_records: list[dict] | None,
                      audits: list[dict] | None,
                      failed: int = 0, invalid: int = 0) -> dict:
    """汇总批次质量计数，供 meta.json 记录；绝不含任何密钥。"""
    records = probe_records or []
    audits = audits or []
    return {
        "request_errors": sum(1 for r in records
                              if r.get("probe_status") == "request_error"),
        "llm_filter_failures": sum(1 for r in records
                                   if r.get("probe_status") == "llm_filter_failed"),
        "weak_evidence": sum(1 for r in records
                             if r.get("probe_status") == "weak_evidence"),
        "confirmed_empty": sum(1 for r in records
                               if r.get("probe_status") == "confirmed_empty"),
        "overall_mismatch": sum(1 for a in audits
                                if a.get("overall_consistent") is False),
        "failed": int(failed),
        "invalid": int(invalid),
    }


def _slim_config(cfg: dict | None) -> dict:
    """批次 meta 里保存的精简配置（不含 api_key）。"""
    if not cfg:
        return {}
    slim = {}
    for section, vals in cfg.items():
        if isinstance(vals, dict):
            slim[section] = {k: v for k, v in vals.items() if k != "api_key"}
        else:
            slim[section] = vals
    return slim


# ============================================================
# 具名读写：活跃批次存在时写入批次目录，否则写顶层（旧行为）
# ============================================================
def save_json(filename: str, data) -> str:
    """原子写入 filename，返回文件路径。"""
    if _active_batch is not None:
        return _active_batch.save_json(filename, data)
    _ensure_dir()
    return _atomic_write_json(DATA_DIR / filename, data)


def load_json(filename: str, default=None):
    """读取 filename；不存在时返回 default（而非抛错）。

    活跃批次优先读批次目录，缺失时回退顶层（兼容继承失败等场景）。
    """
    if _active_batch is not None:
        batch_path = _active_batch.dir / filename
        if batch_path.exists():
            with open(batch_path, encoding="utf-8") as f:
                return json.load(f)
    path = DATA_DIR / filename
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_raw_feed(data) -> str:
    """capture 阶段原始榜单（LLM 处理前的全量快照）。"""
    return save_json("raw_feed.json", data)


def save_benchmarks(data) -> str:
    """capture 阶段最终产物：LLM 精选后的标杆列表。"""
    return save_json("benchmarks.json", data)


def load_benchmarks(default=None):
    """读取标杆列表（M2 probe 阶段的输入）。"""
    return load_json("benchmarks.json", default)


def save_probe(data) -> str:
    """probe 阶段最终产物：鸿蒙侧竞品探测记录。"""
    return save_json("probe.json", data)


def load_probe(default=None):
    """读取探测记录（M3 audit 阶段的输入）。"""
    return load_json("probe.json", default)


def save_opportunities(data) -> str:
    """audit 阶段最终产物：结构化高潜机会清单（仅达标项）。"""
    return save_json("opportunities.json", data)


def load_opportunities(default=None):
    """读取机会清单。"""
    return load_json("opportunities.json", default)


def save_audit_results(data) -> str:
    """audit 阶段完整审计产物：全部有效审计结果（含未达标项）。"""
    return save_json("audit_results.json", data)


def load_audit_results(default=None):
    """读取完整审计结果。"""
    return load_json("audit_results.json", default)


def save_review_queue(data) -> str:
    """audit 阶段人工复核队列：低证据/请求错误/临界项。"""
    return save_json("review_queue.json", data)


def load_review_queue(default=None):
    """读取人工复核队列。"""
    return load_json("review_queue.json", default)


def save_report(markdown: str) -> str:
    """将决策看板 Markdown 内容原子写入 report.md。"""
    if _active_batch is not None:
        return _active_batch.save_report(markdown)
    _ensure_dir()
    return _atomic_write_text(DATA_DIR / "report.md", markdown)


# ============================================================
# 历史批次读取（供 WebUI 回溯）
# ============================================================
def load_run_json(batch_id: str, filename: str, default=None):
    """读取指定批次目录下的 JSON 产物。"""
    path = RUNS_DIR / batch_id / filename
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_run_report(batch_id: str) -> str | None:
    """读取指定批次的 report.md 文本。"""
    path = RUNS_DIR / batch_id / "report.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_traces(batch_id: str) -> list[dict]:
    """读取指定批次的 llm_traces.jsonl（每行一条 JSON）。"""
    path = RUNS_DIR / batch_id / "llm_traces.jsonl"
    if not path.exists():
        return []
    traces = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    traces.append(json.loads(line))
                except (ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return traces


def load_run_prompts(batch_id: str) -> dict:
    """读取指定批次的提示词快照。"""
    return load_run_json(batch_id, "prompts_snapshot.json", {}) or {}