# -*- coding: utf-8 -*-
"""M4: Streamlit GUI 情报看板与交互控制台。

启动方式:
    python -m streamlit run app.py
    python -m radar gui

设计约束（见计划）:
- 后台子进程只向线程安全的 queue.Queue 投递消息，绝不跨线程调用 st.*
- 主线程 / 片段消费队列，更新日志与运行状态
- 只读取 data/ 下已有产物，不改动 schema 与原子写入机制
"""
import html
import json
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from radar import prompts, store, telemetry
from radar.config import DEFAULTS, PROJECT_ROOT, load_config, save_config, validate_config
from radar.pipeline import EXIT_RETRYABLE
from radar.store import DATA_DIR, RUNS_DIR

# Windows asyncio ProactorEventLoop 的已知噪音：对端强制断开（如关闭/刷新
# Streamlit 页面、网络抖动）后，连接收尾时 socket.shutdown() 会抛
# ConnectionResetError，被默认异常处理器打印成 _call_connection_lost 回调错误。
# 它不影响采集/探测/审计流水线，这里仅静音这一种异常，其余照常抛出。
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        _original_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _quiet_call_connection_lost(self, exc):
            try:
                _original_call_connection_lost(self, exc)
            except ConnectionResetError:
                pass

        _ProactorBasePipeTransport._call_connection_lost = _quiet_call_connection_lost
    except Exception:
        pass

st.set_page_config(
    page_title="纯血鸿蒙机会雷达",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------
# 设计 token 与全局样式
# --------------------------------------------------------------------------
CSS = """
<style>
:root{
  color-scheme:dark;
  --bg-0:#05070C; --bg-1:#0A0D12; --surface-1:#0F131C; --surface-2:#141A26;
  --border-1:#1E2735; --border-2:#2A3646;
  --text-1:#E7ECF3; --text-2:#9AA7B8; --text-3:#8A99AC;
  --cyan:#38BDF8; --green:#6EE7B7; --amber:#E9A568; --gold:#FFD54F; --blue:#7DA2FF;
  --danger:#FF8A80; --ok:#6EE7B7;
  --radius:14px; --radius-sm:10px; --ease:cubic-bezier(.22,.61,.36,1);
  --font-display:Georgia,"Times New Roman",serif;
  --font-mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
}
html{ color-scheme:dark; }
.stApp{ background:var(--bg-0); }
.block-container{ padding-top:4.4rem; padding-bottom:2.5rem; max-width:1500px; }
h1,h2,h3,h4{ color:var(--text-1); letter-spacing:.2px; text-wrap:balance; }
p, li, span, label{ color:var(--text-2); }
p{ text-wrap:pretty; }
/* 仅供读屏器：为颜色编码补一段等价文字 */
.sr{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }

/* 浏览器原生表面：选区 / 光标 / 滚动条 / 焦点环 —— 最廉价的"手工打造"信号 */
::selection{ background:rgba(56,189,248,.30); color:var(--text-1); }
input, textarea{ caret-color:var(--cyan); }
::-webkit-scrollbar{ width:10px; height:10px; }
::-webkit-scrollbar-track{ background:var(--bg-1); }
::-webkit-scrollbar-thumb{ background:#26303F; border-radius:999px; border:2px solid var(--bg-1); }
::-webkit-scrollbar-thumb:hover{ background:#33404F; }
*{ scrollbar-color:#26303F var(--bg-1); scrollbar-width:thin; }
:focus-visible{ outline:2px solid var(--cyan); outline-offset:2px; border-radius:4px; }
header[data-testid="stHeader"]{ background:transparent !important; }
section[data-testid="stSidebar"]{ background:var(--bg-1) !important; border-right:1px solid var(--border-1); }

/* 顶部品牌区：报头式排版 —— 衬线题名 + 等宽数据行，其余全部让位给内容 */
.brand{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.brand-mark{ width:38px; height:38px; flex:none; color:var(--cyan); }
.brand-mark svg{ display:block; width:100%; height:100%; }
.brand-title{ font-family:var(--font-display); font-size:1.72rem; font-weight:700;
  color:var(--text-1); line-height:1.12; letter-spacing:.4px; }
.brand-sub{ font-size:.84rem; color:var(--text-3); margin-top:3px; }
.brand-meta{ display:flex; gap:0; flex-wrap:wrap; margin-top:11px; align-items:baseline; }
.brand-meta .item{ font-family:var(--font-mono); font-size:.76rem; color:var(--text-3);
  padding-right:14px; margin-right:14px; border-right:1px solid var(--border-1); }
.brand-meta .item:last-child{ border-right:none; margin-right:0; padding-right:0; }
.brand-meta .item b{ color:var(--text-1); font-weight:650; }

/* 状态胶囊 */
.status{ display:inline-flex; align-items:center; gap:7px; font-size:.78rem; font-weight:600;
  padding:4px 12px; border-radius:999px; border:1px solid var(--border-2); }
.status .dot{ width:7px; height:7px; border-radius:50%; background:var(--text-3); }
.status.idle{ color:var(--text-2); }
.status.running{ color:var(--cyan); border-color:rgba(56,189,248,.45); background:rgba(56,189,248,.08); }
.status.running .dot{ background:var(--cyan); animation:pulse 1.4s var(--ease) infinite; }
.status.done{ color:var(--ok); border-color:rgba(110,231,183,.4); background:rgba(110,231,183,.07); }
.status.done .dot{ background:var(--ok); }
.status.failed{ color:var(--danger); border-color:rgba(255,138,128,.45); background:rgba(255,138,128,.08); }
.status.failed .dot{ background:var(--danger); }
.status.interrupted{ color:var(--amber); border-color:rgba(233,165,104,.45); background:rgba(233,165,104,.08); }
.status.interrupted .dot{ background:var(--amber); }
@keyframes pulse{ 0%,100%{opacity:1} 50%{opacity:.3} }
@media (prefers-reduced-motion:reduce){ *{ animation:none !important; transition:none !important; } }

/* 通用面板 */
.panel{ background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--radius);
  padding:16px 18px; margin-bottom:12px; }
.panel-title{ font-size:.92rem; font-weight:700; color:var(--text-1); margin-bottom:10px; }

/* 运行概览指标条：单一共享表面 + 竖分隔，取代并排卡片。
   颜色只编码"这份数字有多强"，不按指标轮换色相 —— 琥珀留给生态空白 */
.statline{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--radius); }
.stat{ padding:13px 16px; border-left:1px solid var(--border-1); min-width:0; }
.stat:first-child{ border-left:none; }
.stat-k{ font-size:.76rem; color:var(--text-3); }
.stat-v{ font-size:1.7rem; font-weight:700; line-height:1.15; margin-top:3px;
  color:var(--text-1); font-variant-numeric:tabular-nums; }
.stat-v.sig{ color:var(--cyan); }
.stat-v.gap{ color:var(--gold); }
.stat-s{ font-size:.73rem; color:var(--text-3); margin-top:2px; }
@media (max-width:760px){
  .statline{ grid-template-columns:repeat(2,minmax(0,1fr)); }
  .stat{ border-left:none; border-top:1px solid var(--border-1); }
  .stat:nth-child(-n+2){ border-top:none; }
}

/* 天梯：维度名只在表头出现一次，并对齐到四列评分条；行内只留数值与形状 */
.ladder{ border:1px solid var(--border-1); border-radius:var(--radius); overflow:hidden; }
.ladder-head{ padding:10px 14px; background:var(--surface-2); }
.lh-cols{ display:grid; grid-template-columns:2.6rem minmax(0,1.6fr) 5.5rem minmax(0,1fr);
  gap:10px; font-size:.74rem; color:var(--text-3); }
.lh-dims{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px;
  margin-top:10px; font-size:.7rem; color:var(--text-3); }
.lh-dims span b{ display:inline-block; width:7px; height:7px; border-radius:2px;
  margin-right:6px; vertical-align:middle; }
.ladder-row{ padding:12px 14px; border-top:1px solid var(--border-1); background:var(--surface-1);
  transition:background .18s var(--ease); }
.ladder-row.blank{ background:linear-gradient(90deg, rgba(233,165,104,.07), transparent 55%); }
.ladder-row:hover{ background:var(--surface-2); }
.lr-head{ display:grid; grid-template-columns:2.6rem minmax(0,1.6fr) 5.5rem minmax(0,1fr);
  gap:10px; align-items:center; }
.lr-rank{ color:var(--text-3); font-variant-numeric:tabular-nums; font-size:.85rem; }
.lr-name{ color:var(--text-1); font-weight:650; font-size:.95rem;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lr-verdict{ color:var(--text-3); font-size:.78rem; line-height:1.5; }
.lr-bars{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-top:11px; }
@media (max-width:900px){
  .ladder-head{ display:none; }
  .lr-head{ grid-template-columns:2.2rem minmax(0,1fr) auto; }
  .lr-verdict{ grid-column:1 / -1; }
  .lr-bars{ grid-template-columns:repeat(2,minmax(0,1fr)); }
}

/* 评分条：表头已给出维度名，行内标签仅在窄屏（表头隐藏时）回填 */
.bar-wrap{ min-width:0; }
.bar-head{ display:flex; justify-content:space-between; align-items:baseline; gap:8px;
  font-size:.72rem; margin-bottom:4px; min-height:1.05rem; }
.bar-label{ color:var(--text-3); }
.ladder-row .bar-label{ visibility:hidden; }
@media (max-width:900px){ .ladder-row .bar-label{ visibility:visible; } }
.bar-val{ color:var(--text-2); font-variant-numeric:tabular-nums; margin-left:auto; }
.bar-track{ height:6px; border-radius:999px; background:var(--surface-2); overflow:hidden; }
.bar-fill{ height:100%; border-radius:999px; }

/* 徽章与胶囊：分数用等宽数字对齐比较，胶囊只标记"生态空白"这一个特殊状态 */
.badge{ display:inline-block; font-weight:700; font-size:.9rem; padding:2px 11px; border-radius:9px;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.badge.high{ color:#05070C; background:var(--green); }
.badge.mid{ color:#05070C; background:var(--gold); }
.badge.low{ color:var(--text-2); background:var(--surface-2); border:1px solid var(--border-2); }
.pill{ display:inline-block; font-size:.68rem; padding:2px 9px; border-radius:999px;
  border:1px solid var(--border-2); color:var(--text-2); }
.pill-blank{ color:var(--gold); border-color:rgba(255,213,79,.4); background:rgba(255,213,79,.08); }

/* 详情 */
.detail-head{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.detail-name{ font-family:var(--font-display); font-size:1.34rem; font-weight:700;
  color:var(--text-1); line-height:1.2; }
.muted{ color:var(--text-3); font-size:.82rem; line-height:1.55; }
.facts{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-top:6px; }
.fact{ background:var(--surface-2); border:1px solid var(--border-1); border-radius:var(--radius-sm);
  padding:10px 12px; }
.fact-k{ font-size:.72rem; color:var(--text-3); }
.fact-v{ font-size:1.05rem; font-weight:650; color:var(--text-1); margin-top:2px;
  font-variant-numeric:tabular-nums; }
.comp-row{ padding:7px 0 7px 12px; margin-bottom:8px; border-left:1px solid var(--border-2); }
.comp-name{ color:var(--text-1); font-weight:600; font-size:.9rem; }
.comp-meta{ color:var(--text-3); font-size:.76rem; font-variant-numeric:tabular-nums; }
.comp-meta span{ margin-right:14px; }
.pain{ color:var(--amber); font-size:.78rem; margin-top:4px; }
.verdict{ background:var(--surface-2); border:1px solid var(--border-1);
  border-radius:var(--radius-sm); padding:12px 14px; color:var(--text-1); font-size:.9rem;
  line-height:1.6; }

/* 详情/空白卡片容器：st.container(key=...) 渲染为 st-key-* 类，避免手工开合 div */
.st-key-detail_card, [class*="st-key-gap_card_"]{
  background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--radius);
  padding:18px 20px; margin-bottom:14px; }
[class*="st-key-gap_card_"]{
  background:linear-gradient(90deg, rgba(233,165,104,.06), transparent 40%), var(--surface-1); }

/* 数据产物状态 */
.artifact{ display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding:9px 12px; border-bottom:1px solid var(--border-1); font-size:.82rem; }
.artifact:last-child{ border-bottom:none; }
.artifact .name{ color:var(--text-1); font-weight:600; }
.artifact .meta{ color:var(--text-3); font-size:.76rem; text-align:right; }
.artifact .state{ display:inline-flex; align-items:center; gap:6px; font-size:.76rem; }
.artifact .state .dot{ width:6px; height:6px; border-radius:50%; background:currentColor; }
.artifact .state.ok{ color:var(--ok); }
.artifact .state.bad{ color:var(--danger); }

/* 日志 */
.logbox{ background:#04060A; border:1px solid var(--border-1); border-radius:var(--radius-sm);
  padding:12px 14px; font-family:var(--font-mono); font-size:.76rem;
  line-height:1.55; color:#B9C6D6; max-height:420px; overflow-y:auto; white-space:pre-wrap;
  word-break:break-word; overscroll-behavior:contain; }
.logbox.small{ max-height:150px; }

/* 非线性缓动进度条：超平滑弹力缓动 + 流动渐变微光，避免 Streamlit 原生动画的生硬跳变 */
.prog{ margin-top:12px; }
.prog-head{ display:flex; justify-content:space-between; align-items:baseline; gap:12px;
  font-size:.8rem; margin-bottom:7px; }
.prog-label{ color:var(--text-1); font-weight:650; }
.prog-stage{ color:var(--text-3); font-family:var(--font-mono); font-size:.74rem; }
.prog-pct{ color:var(--cyan); font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  font-weight:700; }
.prog-track{ height:9px; border-radius:999px; background:var(--surface-2);
  overflow:hidden; border:1px solid var(--border-1); }
.prog-fill{ height:100%; border-radius:999px; position:relative;
  background:linear-gradient(90deg,#38BDF8,#6EE7B7);
  transition:width .6s cubic-bezier(.16,1,.3,1);
  box-shadow:0 0 14px rgba(56,189,248,.35); }
.prog-fill::after{ content:""; position:absolute; inset:0;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);
  background-size:200% 100%; animation:shimmer 1.8s linear infinite; }
@keyframes shimmer{ 0%{background-position:200% 0} 100%{background-position:-200% 0} }
.prog-meta{ color:var(--text-3); font-size:.74rem; margin-top:6px; font-family:var(--font-mono); }
@media (prefers-reduced-motion:reduce){
  .prog-fill{ transition:none; } .prog-fill::after{ animation:none; }
}

/* 结构化 LLM Traces 卡片：标注耗时/状态/标杆，点击展开完整 Prompt 与原始响应 */
.trace{ background:var(--surface-1); border:1px solid var(--border-1);
  border-radius:var(--radius-sm); padding:10px 13px; margin-bottom:8px; }
.trace-head{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.trace-stage{ font-family:var(--font-mono); font-size:.72rem; color:var(--text-3);
  border:1px solid var(--border-2); border-radius:999px; padding:1px 9px; }
.trace-target{ color:var(--text-1); font-weight:650; font-size:.86rem; }
.trace-dur{ color:var(--text-3); font-family:var(--font-mono); font-size:.74rem;
  margin-left:auto; font-variant-numeric:tabular-nums; }
.trace-st{ font-size:.72rem; font-weight:650; }
.trace-st.ok{ color:var(--ok); } .trace-st.err{ color:var(--danger); }
.trace-body{ margin-top:8px; }
.trace-sec{ margin-top:8px; }
.trace-sec .k{ font-size:.72rem; color:var(--text-3); margin-bottom:3px; }
.trace-pre{ background:#04060A; border:1px solid var(--border-1); border-radius:8px;
  padding:9px 11px; font-family:var(--font-mono); font-size:.73rem; color:#B9C6D6;
  max-height:260px; overflow:auto; white-space:pre-wrap; word-break:break-word; }

/* 提示词编辑器 */
.prompt-head{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
.prompt-name{ color:var(--text-1); font-weight:650; }
.ph-chip{ font-family:var(--font-mono); font-size:.7rem; color:var(--cyan);
  background:rgba(56,189,248,.08); border:1px solid rgba(56,189,248,.3);
  border-radius:6px; padding:1px 7px; margin-right:5px; }
.custom-badge{ font-size:.68rem; color:var(--gold); border:1px solid rgba(255,213,79,.4);
  background:rgba(255,213,79,.08); border-radius:999px; padding:1px 9px; }

/* 空状态：教会用户下一步，而不是"这里什么都没有" */
.empty{ border:1px dashed var(--border-2); border-radius:var(--radius); padding:26px 22px;
  text-align:center; background:var(--surface-1); }
.empty .t{ color:var(--text-1); font-weight:650; font-size:.98rem; }
.empty .d{ color:var(--text-3); font-size:.82rem; margin-top:6px; }
.empty .cmd{ display:inline-block; margin-top:10px; background:var(--bg-0); border:1px solid var(--border-2);
  border-radius:8px; padding:5px 11px; color:var(--cyan); font-size:.78rem;
  font-family:var(--font-mono); }

/* 标签页与控件 */
.stTabs [data-baseweb="tab-list"]{ gap:6px; border-bottom:1px solid var(--border-1); }
.stTabs [data-baseweb="tab"]{ padding:9px 16px; border-radius:10px 10px 0 0; color:var(--text-2);
  transition:color .18s var(--ease), background .18s var(--ease); }
.stTabs [data-baseweb="tab"]:hover{ color:var(--text-1); }
.stTabs [aria-selected="true"]{ color:var(--text-1) !important; background:var(--surface-1); font-weight:650; }
div[data-testid="stExpander"] details{ border:1px solid var(--border-1) !important; border-radius:var(--radius-sm) !important; }
hr{ border-color:var(--border-1); }

/* 按钮：统一形状与状态，主按钮用行动色；触屏去掉 300ms 双击缩放延迟 */
.stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] > button{
  border-radius:10px; border:1px solid var(--border-2); font-weight:600;
  touch-action:manipulation; -webkit-tap-highlight-color:transparent;
  transition:border-color .18s var(--ease), background .18s var(--ease), color .18s var(--ease); }
.stButton > button:hover:not(:disabled), .stDownloadButton > button:hover:not(:disabled){
  border-color:var(--cyan); color:var(--cyan); }
.stButton > button:disabled, [data-testid="stFormSubmitButton"] > button:disabled{ opacity:.42; }
[data-testid="stFormSubmitButton"] > button[kind="primary"],
.stButton > button[kind="primary"]{ background:var(--cyan); border-color:var(--cyan); color:#05070C; }
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover,
.stButton > button[kind="primary"]:hover:not(:disabled){ background:#5CC9F5; border-color:#5CC9F5; color:#05070C; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# --------------------------------------------------------------------------
# 会话状态
# --------------------------------------------------------------------------
LOG_KEY = "_log"
RUN_KEY = "_run"
QUEUE_KEY = "_queue"
TRACES_KEY = "_traces"
PROGRESS_KEY = "_progress"
HISTORY_KEY = "_history_batch"
PROMPT_DRAFT_KEY = "_prompt_draft"
MIN_SCORE_KEY = "min_score"
MAX_LOG = 600
MAX_TRACES = 200
DIM_META = [
    ("需求验证", "demand", "#38BDF8", "场景普遍性与刚需度"),
    ("体验落差", "experience_gap", "#6EE7B7", "鸿蒙供给空白 / 竞品差评空间"),
    ("原生契合", "native_advantage", "#A78BFA", "实况窗 / 卡片 / 小艺 / 端侧 AI"),
    ("开发可行", "indie_feasibility", "#94A3B8", "单人端侧开发成本"),
]

PIPELINE_STEPS = {
    "full": [("榜单捕获", ["capture"]), ("供给探测", ["probe"]), ("四维审计", ["audit"])],
    "capture": [("榜单捕获", ["capture"])],
    "probe": [("供给探测", ["probe"])],
    "audit": [("四维审计", ["audit"])],
}

# 步骤级可重试中断的自动重试次数与退避基数（秒）
STEP_RETRY_ATTEMPTS = 2
STEP_RETRY_BASE_WAIT = 3.0

# 历史批次下拉的「当前/最新」哨兵值
LATEST_SENTINEL = "__latest__"


def _init_session():
    st.session_state.setdefault(LOG_KEY, [])
    st.session_state.setdefault(
        RUN_KEY,
        {"status": "idle", "label": "", "cmd": "", "start": None, "end": None,
         "returncode": None, "error": None, "batch_id": None, "key": None,
         "steps": [], "failed_step": None},
    )
    st.session_state.setdefault(QUEUE_KEY, None)
    st.session_state.setdefault(TRACES_KEY, [])
    st.session_state.setdefault(
        PROGRESS_KEY,
        {"pct": 0.0, "label": "", "step": "", "current": "", "message": "", "stage": ""},
    )


def _append_log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    logs = st.session_state[LOG_KEY]
    logs.append(line)
    if len(logs) > MAX_LOG:
        del logs[:-MAX_LOG]


def _append_trace(trace: dict):
    traces = st.session_state[TRACES_KEY]
    traces.append(trace)
    if len(traces) > MAX_TRACES:
        del traces[:-MAX_TRACES]


# --------------------------------------------------------------------------
# 后台执行（线程安全：只写 queue，不碰 st.*）
# --------------------------------------------------------------------------
def _run_step(q: "queue.Queue", prefix: str, args: list, batch_id: str | None):
    """执行单个阶段子进程，返回 (returncode, batch_id)。

    子进程 stdout 的遥测事件实时投递到 queue；批次号在首次出现时捕获。
    """
    cmd_args = list(args) + ["--emit-events"]
    if batch_id:
        cmd_args += ["--batch-id", batch_id]
    q.put(("log", f"▶ {prefix}  (python -m radar {' '.join(cmd_args)})"))
    step_started = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "radar", *cmd_args],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:  # 启动失败：解释器缺失 / 环境异常
        q.put(("error", f"命令执行失败，请检查环境: {exc}"))
        return None, batch_id
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                continue
            event = telemetry.parse_event_line(line)
            if event is None:
                q.put(("log", line.rstrip()))
                continue
            # 捕获首个阶段的批次号，供后续阶段复用同一批次目录
            if batch_id is None and event.get("batch_id"):
                batch_id = event["batch_id"]
            q.put(("event", event))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    # 读到 stdout EOF 后 returncode 仍可能是 None（进程尚未回收），
    # 必须显式 wait() 拿真实退出码；否则 None != 0 会把成功误判为失败。
    try:
        returncode = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = proc.wait()
    except Exception:
        returncode = proc.poll()
    elapsed = time.time() - step_started
    if returncode is None:
        q.put(("error", f"{prefix} 未能获取退出码，进程可能异常退出。"))
        return None, batch_id
    q.put(("log", f"■ {prefix} 结束（退出码 {returncode}，耗时 {elapsed:.1f}s）"))
    return returncode, batch_id


def _worker(q: "queue.Queue", label: str, steps: list, offset: int = 0,
            batch_id: str | None = None):
    """后台线程：串行执行各阶段子进程，解析其 stdout 遥测事件并投递到 queue。

    事件协议（见 radar/telemetry.py）: 行首 `__RADAR_EVENT__:{json}` 为结构化事件，
    其余行为普通日志。全量扫描时各阶段共享同一个 batch_id，产物归档到同一批次。

    offset 为续跑时的全局步骤起点（用于显示「步骤 2/3」）；batch_id 续跑时复用。

    失败语义：
    - 退出码 2（可重试中断）自动重试 STEP_RETRY_ATTEMPTS 次，仍失败则进入
      「中断」态并上报失败步骤索引，等待用户在 WebUI 修好配置后一键继续。
    - 其他非零退出码为致命熔断，直接中止。
    """
    total = offset + len(steps)
    for i, (step_label, args) in enumerate(steps, offset + 1):
        prefix = f"步骤 {i}/{total} · {step_label}" if total > 1 else step_label
        attempt = 0
        while True:
            attempt += 1
            attempt_prefix = (f"{prefix}（第 {attempt} 次尝试）"
                              if attempt > 1 else prefix)
            returncode, batch_id = _run_step(q, attempt_prefix, args, batch_id)
            if returncode is None:
                q.put(("done", -1))
                return
            if returncode == 0:
                break
            # 可重试中断：退避后自动重试，仍失败则交回用户决定是否继续
            if returncode == EXIT_RETRYABLE and attempt <= STEP_RETRY_ATTEMPTS:
                wait = STEP_RETRY_BASE_WAIT * attempt
                q.put(("log", f"↻ {step_label} 可重试中断，{wait:.0f}s 后自动重试"
                              f"（{attempt}/{STEP_RETRY_ATTEMPTS}）…"))
                time.sleep(wait)
                continue
            if returncode == EXIT_RETRYABLE:
                q.put(("error", f"{step_label} 重试 {STEP_RETRY_ATTEMPTS} 次后仍中断，"
                                f"已保留历史产物。修正配置后可点「继续」从该步恢复。"))
                q.put(("interrupted", {"step_index": i - 1, "batch_id": batch_id,
                                       "label": label}))
                return
            q.put(("error", f"{step_label} 退出码 {returncode}，流程中止。"))
            q.put(("done", returncode))
            return
    q.put(("done", 0))


def _start_run(label: str, key: str, resume_from: int = 0, batch_id: str | None = None,
               all_steps: list | None = None):
    """启动一次运行。

    resume_from > 0 表示从失败步骤继续：只跑该步及其之后的步骤，
    并复用原批次目录（batch_id），上游产物已在其中，无需重新继承。
    all_steps 用于在续跑时保留完整步骤列表（进度显示为「步骤 2/3」而非「1/2」）。
    """
    full_steps = all_steps or PIPELINE_STEPS[key]
    steps = full_steps[resume_from:]
    q: "queue.Queue" = queue.Queue()
    st.session_state[QUEUE_KEY] = q
    st.session_state[LOG_KEY] = []
    st.session_state[TRACES_KEY] = []
    st.session_state[PROGRESS_KEY] = {
        "pct": 0.0, "label": "", "step": "", "current": "", "message": "", "stage": ""}
    st.session_state[RUN_KEY] = {
        "status": "running",
        "label": label,
        "cmd": "python -m radar " + " ".join(a for _, args in full_steps for a in args),
        "start": time.time(),
        "end": None,
        "returncode": None,
        "error": None,
        "batch_id": batch_id,
        "key": key,
        "steps": [s[0] for s in full_steps],
        "failed_step": None,
    }
    verb = f"继续: {label}（从步骤 {resume_from + 1}/{len(full_steps)}）" if resume_from else f"启动: {label}"
    _append_log(verb)
    threading.Thread(target=_worker, args=(q, label, steps, resume_from, batch_id),
                     daemon=True).start()


def _resume_run():
    """从上次中断的步骤继续：复用原批次目录，只跑失败步骤及其后续。"""
    state = st.session_state[RUN_KEY]
    key = state.get("key")
    failed_step = state.get("failed_step")
    if not key or failed_step is None:
        return
    _start_run(state.get("label", "继续"), key, resume_from=failed_step,
               batch_id=state.get("batch_id"), all_steps=PIPELINE_STEPS[key])


def _handle_event(event: dict):
    """把一条遥测事件应用到会话状态（进度 / trace / 批次号）。"""
    etype = event.get("type")
    state = st.session_state[RUN_KEY]
    if event.get("batch_id") and not state.get("batch_id"):
        state["batch_id"] = event["batch_id"]
    if etype == "progress":
        st.session_state[PROGRESS_KEY] = {
            "pct": float(event.get("pct", 0.0)),
            "label": event.get("label", ""),
            "stage": event.get("stage", ""),
            "step": f"{event.get('index', 0)}/{event.get('total', 0)}",
            "current": event.get("current", ""),
            "message": event.get("message", ""),
        }
    elif etype == "stage":
        prog = dict(st.session_state[PROGRESS_KEY])
        if event.get("status") == "start":
            prog.update({"label": event.get("label", ""), "stage": event.get("stage", ""),
                         "step": f"0/{event.get('total', 0)}", "current": "",
                         "message": "开始…"})
        else:
            prog.update({"pct": float(event.get("pct", prog.get("pct", 0.0))),
                         "message": event.get("message", "")})
        st.session_state[PROGRESS_KEY] = prog
    elif etype == "trace":
        _append_trace(event)
    elif etype == "log":
        _append_log(event.get("message", ""))
    elif etype == "run":
        st.session_state[PROGRESS_KEY] = {
            **st.session_state[PROGRESS_KEY], "pct": 100.0,
            "message": event.get("message", ""),
        }


def _drain_queue():
    q = st.session_state.get(QUEUE_KEY)
    if q is None:
        return
    state = st.session_state[RUN_KEY]
    while True:
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            break
        if kind == "log":
            _append_log(payload)
        elif kind == "event":
            _handle_event(payload)
        elif kind == "error":
            state["error"] = payload
            _append_log(f"✖ {payload}")
        elif kind == "interrupted":
            state["status"] = "interrupted"
            state["end"] = time.time()
            state["failed_step"] = payload.get("step_index", 0)
            if payload.get("batch_id"):
                state["batch_id"] = payload["batch_id"]
            _append_log("已中断，等待修正配置后继续。")
        elif kind == "done":
            state["returncode"] = payload
            state["end"] = time.time()
            if payload == 0 and not state.get("error"):
                state["status"] = "done"
                _append_log("完成（返回码 0）")
            else:
                state["status"] = "failed"
                _append_log(f"失败（返回码 {payload}）")
    if state["status"] in ("done", "failed", "interrupted"):
        st.session_state[QUEUE_KEY] = None


def _is_running() -> bool:
    return st.session_state.get(RUN_KEY, {}).get("status") == "running"


def _elapsed(state) -> str:
    if not state.get("start"):
        return ""
    end = state.get("end") or time.time()
    secs = int(end - state["start"])
    return f"{secs // 60}分{secs % 60:02d}秒"


def _status_html(state, live: bool = True) -> str:
    status = state.get("status", "idle")
    label = {"idle": "空闲", "running": "运行中", "done": "已完成",
             "failed": "失败", "interrupted": "已中断"}.get(status, status)
    if status == "running":
        label = f"运行中 · {state.get('label', '')}"
    live_attr = ' role="status" aria-live="polite"' if live else ""
    return (f'<span class="status {status}"{live_attr}>'
            f'<span class="dot" aria-hidden="true"></span>{html.escape(label)}</span>')


# --------------------------------------------------------------------------
# 数据加载（全部走已有产物，损坏时给出明确错误态）
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _read_json_cached(path_str: str, mtime_ns: int, size: int):
    """按 (路径, mtime, size) 缓存的 JSON 读取，避免每次 rerun 重解析大产物。"""
    try:
        with open(path_str, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as exc:
        name = Path(path_str).name
        return None, f"数据文件损坏，请重新扫描（{name}: {exc}）"


def _read_json(filename: str):
    """返回 (data, error)。文件不存在时 data 为 None 且无 error。

    产物由原子替换写入，mtime/size 变化即触发缓存失效，无需手动清理。
    """
    path = DATA_DIR / filename
    if not path.exists():
        return None, None
    stat = path.stat()
    return _read_json_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _read_run_json(batch_id: str, filename: str):
    """读取指定历史批次的 JSON 产物，返回 (data, error)。"""
    path = RUNS_DIR / batch_id / filename
    if not path.exists():
        return None, None
    stat = path.stat()
    return _read_json_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _load_all(batch_id: str | None = None):
    """加载产物。batch_id 为 None 时读 data/ 顶层最新镜像，否则读历史批次。"""
    reader = (lambda fn: _read_run_json(batch_id, fn)) if batch_id else _read_json
    feed, feed_err = reader("raw_feed.json")
    bench, bench_err = reader("benchmarks.json")
    probe, probe_err = reader("probe.json")
    opps, opp_err = reader("opportunities.json")
    audits, audits_err = reader("audit_results.json")
    review, review_err = reader("review_queue.json")
    errors = [e for e in (feed_err, bench_err, probe_err, opp_err,
                          audits_err, review_err) if e]
    return feed, bench, probe, opps, audits, review, errors


def _history_options() -> list[str]:
    """历史批次下拉选项（时间倒序，含状态标记）。"""
    opts = []
    for item in store.list_runs():
        meta = item.get("meta", {})
        status = {"success": "成功", "failed": "失败", "running": "运行中",
                  "interrupted": "中断"}.get(meta.get("status"), "未知")
        stage = meta.get("stage", "?")
        opts.append(f"{item['batch_id']} · {stage} · {status}")
    return opts


def _history_batch_id(label: str) -> str | None:
    """从下拉标签还原 batch_id。"""
    if not label:
        return None
    return label.split(" · ", 1)[0]


def _artifact_rows(batch_id: str | None = None):
    specs = [
        ("raw_feed.json", "原始榜单", "captured_at", "apps"),
        ("benchmarks.json", "精选标杆", "captured_at", "benchmarks"),
        ("probe.json", "鸿蒙供给探测", "probed_at", "benchmarks"),
        ("opportunities.json", "机会审计结果", "audited_at", "opportunities"),
        ("audit_results.json", "完整审计结果", "audited_at", "audits"),
        ("review_queue.json", "人工复核队列", "generated_at", "items"),
    ]
    rows = []
    for filename, title, ts_key, list_key in specs:
        path = (RUNS_DIR / batch_id / filename) if batch_id else (DATA_DIR / filename)
        data, err = (_read_run_json(batch_id, filename) if batch_id
                     else _read_json(filename))
        if err:
            rows.append({"file": filename, "title": title, "ok": False, "count": None,
                         "ts": "", "note": "文件损坏"})
            continue
        if data is None:
            rows.append({"file": filename, "title": title, "ok": False, "count": None,
                         "ts": "", "note": "未生成"})
            continue
        if isinstance(data, dict):
            lst = data.get(list_key)
            count = data.get("count")
            if count is None and isinstance(lst, list):
                count = len(lst)
            ts = data.get(ts_key, "") or ""
        elif isinstance(data, list):
            count, ts = len(data), ""
        else:
            count, ts = None, ""
        rows.append({"file": filename, "title": title, "ok": True, "count": count,
                     "ts": ts, "note": ""})
    return rows


def _fmt_ts(ts: str) -> str:
    """ISO 时间戳 → 本地可读格式；解析失败时原样回退，不丢信息。"""
    if not ts:
        return "—"
    raw = str(ts)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw.replace("T", " ").replace("+08:00", "")


# --------------------------------------------------------------------------
# 通用渲染片段
# --------------------------------------------------------------------------
def _clip(text, n):
    text = (text or "").strip()
    if not text:
        return ""
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _score_class(v):
    if v >= 8.0:
        return "high"
    if v >= 6.0:
        return "mid"
    return "low"


def _overall(opp) -> float:
    """容错读取综合分：字段缺失或非数值时按 0 处理，避免整页渲染中断。"""
    try:
        return float(opp.get("scores", {}).get("overall", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _bar(label, value, color):
    try:
        val = float(value)
    except (TypeError, ValueError):
        val = 0.0
    pct = max(0.0, min(1.0, val / 10.0)) * 100
    return (
        f'<div class="bar-wrap">'
        f'<div class="bar-head"><span class="bar-label">{html.escape(label)}</span>'
        f'<span class="bar-val">{val:.1f}</span></div>'
        f'<div class="bar-track" role="img" aria-label="{html.escape(label)} {val:.1f} 分">'
        f'<div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div></div>'
    )


def _empty_state(title, detail, cmd=None):
    cmd_html = f'<div class="cmd">{html.escape(cmd)}</div>' if cmd else ""
    st.markdown(
        f'<div class="empty"><div class="t">{html.escape(title)}</div>'
        f'<div class="d">{html.escape(detail)}</div>{cmd_html}</div>',
        unsafe_allow_html=True,
    )


BRAND_MARK = (
    '<svg viewBox="0 0 40 40" fill="none" aria-hidden="true">'
    '<circle cx="20" cy="20" r="15.5" stroke="#1E2735" stroke-width="1.5"/>'
    '<circle cx="20" cy="20" r="9" stroke="#2A3646" stroke-width="1.5"/>'
    '<path d="M20 20 L20 4.5 A15.5 15.5 0 0 1 35.5 20 Z" fill="rgba(56,189,248,.16)"/>'
    '<path d="M20 20 L20 4.5" stroke="#38BDF8" stroke-width="2" stroke-linecap="round"/>'
    '<path d="M20 20 L31 29" stroke="#6EE7B7" stroke-width="2" stroke-linecap="round"/>'
    '<circle cx="20" cy="20" r="2.6" fill="#E7ECF3"/>'
    '</svg>'
)


def _passed(opportunities, min_score):
    return [o for o in opportunities if _overall(o) >= min_score]


# --------------------------------------------------------------------------
# 顶部品牌区
# --------------------------------------------------------------------------
def render_brand(state, opp_meta, min_score, opportunities):
    rows = _artifact_rows()
    ready = sum(1 for r in rows if r["ok"])
    left, right = st.columns([3.2, 1])
    with left:
        st.markdown(
            '<div class="brand">'
            f'<div class="brand-mark">{BRAND_MARK}</div>'
            '<div><div class="brand-title">纯血鸿蒙机会雷达</div>'
            '<div class="brand-sub">双端供给落差分析 · 从 iOS 精品标杆推导鸿蒙原生机会</div></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        audited_at = _fmt_ts(opp_meta.get("audited_at", "")) if opp_meta else "—"
        model = (opp_meta.get("model") or "—") if opp_meta else "—"
        st.markdown(
            '<div class="brand-meta">'
            f'<div class="item">最近审计 <b>{html.escape(audited_at)}</b></div>'
            f'<div class="item">审计模型 <b>{html.escape(str(model))}</b></div>'
            f'<div class="item">数据产物 <b>{ready}/4 就绪</b></div>'
            '</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            f'<div style="text-align:right;padding-top:6px">{_status_html(state, live=False)}</div>',
            unsafe_allow_html=True,
        )
        if st.button("刷新数据", use_container_width=True, disabled=_is_running()):
            st.rerun()


def render_history_selector() -> str | None:
    """顶栏历史批次回溯器：返回选中的 batch_id（None 表示最新镜像）。"""
    options = _history_options()
    if not options:
        return None
    labels = ["最新（data/ 顶层镜像）"] + options
    current = st.session_state.get(HISTORY_KEY, LATEST_SENTINEL)
    try:
        index = labels.index(current)
    except ValueError:
        index = 0
    choice = st.selectbox(
        "历史批次回溯", labels, index=index, key="_history_select",
        help="切换查看任意历史批次的归档产物；「最新」读取 data/ 顶层镜像。",
    )
    st.session_state[HISTORY_KEY] = choice
    if choice == labels[0]:
        return None
    return _history_batch_id(choice)


# --------------------------------------------------------------------------
# 运行状态条 + 实时进度 + 结构化日志（片段：仅在运行时轮询）
# --------------------------------------------------------------------------
def _progress_html(prog: dict) -> str:
    """渲染非线性缓动进度条（纯 CSS 动画，无需 JS）。"""
    pct = max(0.0, min(100.0, float(prog.get("pct", 0.0))))
    label = prog.get("label") or "准备中"
    stage = prog.get("stage", "")
    step = prog.get("step", "")
    current = prog.get("current", "")
    message = prog.get("message", "")
    stage_txt = " · ".join(x for x in (stage, step) if x)
    meta_bits = [b for b in (current, message) if b]
    meta = " · ".join(meta_bits)
    return (
        f'<div class="prog">'
        f'<div class="prog-head"><span class="prog-label">{html.escape(str(label))}</span>'
        f'<span class="prog-stage">{html.escape(stage_txt)}</span>'
        f'<span class="prog-pct">{pct:.0f}%</span></div>'
        f'<div class="prog-track" role="progressbar" aria-valuenow="{pct:.0f}" '
        f'aria-valuemin="0" aria-valuemax="100">'
        f'<div class="prog-fill" style="width:{pct:.1f}%"></div></div>'
        f'{f"<div class=\'prog-meta\'>{html.escape(meta)}</div>" if meta else ""}'
        f'</div>'
    )


def _trace_card(trace: dict, idx: int) -> str:
    """渲染一张结构化 LLM 调用卡片（原生 details 折叠，无需 JS）。"""
    ok = trace.get("status") == "success"
    st_cls, st_txt = ("ok", "成功") if ok else ("err", "失败")
    stage = html.escape(str(trace.get("stage") or "—"))
    target = html.escape(str(trace.get("target_name") or "—"))
    dur = trace.get("duration_s", 0)
    err = trace.get("error")
    sys_p = html.escape(str(trace.get("system_prompt") or ""))
    usr_p = html.escape(str(trace.get("user_prompt") or ""))
    raw = trace.get("raw_response")
    raw_txt = html.escape(str(raw)) if raw is not None else "（无响应）"
    body = (
        f'<div class="trace-body">'
        f'<div class="trace-sec"><div class="k">System Prompt</div>'
        f'<div class="trace-pre">{sys_p or "（空）"}</div></div>'
        f'<div class="trace-sec"><div class="k">User Prompt</div>'
        f'<div class="trace-pre">{usr_p or "（空）"}</div></div>'
        f'<div class="trace-sec"><div class="k">Raw Response</div>'
        f'<div class="trace-pre">{raw_txt}</div></div>'
        + (f'<div class="trace-sec"><div class="k">Error</div>'
           f'<div class="trace-pre">{html.escape(str(err))}</div></div>' if err else "")
        + '</div>'
    )
    return (
        f'<details class="trace"><summary class="trace-head">'
        f'<span class="trace-stage">{stage}</span>'
        f'<span class="trace-target">{target}</span>'
        f'<span class="trace-st {st_cls}">{st_txt}</span>'
        f'<span class="trace-dur">{float(dur):.2f}s</span>'
        f'</summary>{body}</details>'
    )


@st.fragment(run_every=1.0)
def _live_fragment():
    _drain_queue()
    state = st.session_state[RUN_KEY]
    if state["status"] != "running":
        st.rerun(scope="app")
        return
    prog = st.session_state[PROGRESS_KEY]
    logs = st.session_state[LOG_KEY]
    tail = "\n".join(logs[-10:]) if logs else "等待输出…"
    st.markdown(
        f'<div class="panel"><div style="display:flex;justify-content:space-between;'
        f'align-items:center;gap:12px;flex-wrap:wrap">'
        f'<div>{_status_html(state)}</div>'
        f'<div class="muted">{html.escape(state.get("cmd", ""))} · 已运行 {_elapsed(state)}</div>'
        f'</div>{_progress_html(prog)}'
        f'<div class="logbox small" role="log" aria-live="polite" '
        f'style="margin-top:10px">{html.escape(tail)}</div></div>',
        unsafe_allow_html=True,
    )


def _render_resume_controls(state):
    """中断态下的继续控件：显示失败步骤并提供「继续」按钮。"""
    key = state.get("key")
    failed_step = state.get("failed_step") or 0
    steps = PIPELINE_STEPS.get(key, [])
    step_label = steps[failed_step][0] if failed_step < len(steps) else "未知步骤"
    st.markdown(
        f'<div class="muted" style="margin-top:8px">'
        f'将在同一批次 <b>{html.escape(str(state.get("batch_id") or "—"))}</b> 中'
        f'从「{html.escape(step_label)}」重新执行。请先到「控制台」修正配置。'
        f'</div>',
        unsafe_allow_html=True,
    )
    if st.button("继续执行（从失败步骤恢复）", type="primary", use_container_width=False):
        _resume_run()
        st.rerun()


def render_run_strip():
    state = st.session_state[RUN_KEY]
    if state["status"] == "running":
        _live_fragment()
        return
    if state["status"] in ("done", "failed", "interrupted"):
        prog = st.session_state[PROGRESS_KEY]
        traces = st.session_state[TRACES_KEY]
        batch_id = state.get("batch_id")
        badge = (f' · 批次 <b>{html.escape(str(batch_id))}</b>' if batch_id else "")
        border = {"failed": "rgba(255,138,128,.45)",
                  "interrupted": "rgba(233,165,104,.45)"}.get(
                      state["status"], "var(--border-1)")
        err_html = ""
        if state.get("error"):
            err_html = (f'<div class="muted" style="color:var(--danger);margin-top:8px">'
                        f'{html.escape(state["error"])}</div>')
        st.markdown(
            f'<div class="panel" style="border-color:{border}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'gap:12px;flex-wrap:wrap">'
            f'<div>{_status_html(state, live=False)}</div>'
            f'<div class="muted">{html.escape(state.get("label", ""))} · 耗时 {_elapsed(state)}'
            f'{badge}</div></div>'
            f'{_progress_html(prog)}{err_html}</div>',
            unsafe_allow_html=True,
        )
        if state["status"] == "interrupted":
            _render_resume_controls(state)
        if traces:
            with st.expander(f"LLM 调用追踪（{len(traces)} 次，点击展开详情）", expanded=False):
                st.markdown("".join(_trace_card(t, i) for i, t in enumerate(traces)),
                            unsafe_allow_html=True)


# --------------------------------------------------------------------------
# 侧栏
# --------------------------------------------------------------------------
def sidebar_panel(cfg):
    st.sidebar.markdown(
        '<div class="brand-title" style="font-size:1.05rem">纯血鸿蒙机会雷达</div>'
        '<div class="brand-sub" style="font-size:.72rem">M4 · 情报看板</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(_status_html(st.session_state[RUN_KEY], live=False),
                        unsafe_allow_html=True)
    st.sidebar.markdown("---")

    default_score = float(st.session_state.get(MIN_SCORE_KEY, cfg.get("audit", {}).get("min_score", 6.0)))
    min_score = st.sidebar.slider("最低分门槛", 0.0, 10.0, value=default_score, step=0.5)
    st.session_state[MIN_SCORE_KEY] = min_score
    st.sidebar.caption("仅影响看板展示，不改变已写入的审计结果。")

    st.sidebar.markdown("---")
    st.sidebar.markdown("**扫描**")
    disabled = _is_running()
    if st.sidebar.button("全量扫描（捕获→探测→审计）", disabled=disabled, use_container_width=True):
        _start_run("全量扫描", "full")
        st.rerun()
    c1, c2 = st.sidebar.columns(2)
    if c1.button("仅捕获", disabled=disabled, use_container_width=True):
        _start_run("仅捕获", "capture")
        st.rerun()
    if c2.button("仅探测", disabled=disabled, use_container_width=True):
        _start_run("仅探测", "probe")
        st.rerun()
    if st.sidebar.button("仅审计", disabled=disabled, use_container_width=True):
        _start_run("仅审计", "audit")
        st.rerun()

    if st.session_state[RUN_KEY].get("status") == "interrupted":
        if st.sidebar.button("继续上次中断的流程", type="primary",
                             disabled=disabled, use_container_width=True, key="side_resume"):
            _resume_run()
            st.rerun()

    st.sidebar.markdown("---")
    logs = st.session_state[LOG_KEY]
    st.sidebar.markdown("**最近日志**")
    st.sidebar.markdown(
        f'<div class="logbox small">{html.escape(logs[-1] if logs else "暂无日志")}</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.caption("完整日志见「控制台」标签。")
    return min_score


# --------------------------------------------------------------------------
# 标签 1：机会总览
# --------------------------------------------------------------------------
def render_kpi_row(opportunities, min_score, audit_meta=None, review_meta=None,
                   probe_data=None):
    passed = _passed(opportunities, min_score)
    blanks = [o for o in passed if o.get("is_blank")]
    high = [o for o in passed if _overall(o) >= 8.0]
    avg = sum(_overall(o) for o in passed) / len(passed) if passed else 0.0
    total_audited = (audit_meta or {}).get("total_audited", len(opportunities))
    review_count = (review_meta or {}).get("count", 0)
    # 生态空白与请求错误来自 probe 状态，绝不含糊
    probe_records = (probe_data or {}).get("benchmarks", []) if isinstance(probe_data, dict) else []
    confirmed_empty = sum(1 for r in probe_records
                          if r.get("probe_status") == "confirmed_empty")
    request_errors = sum(1 for r in probe_records
                         if r.get("probe_status") == "request_error")
    cards = [
        ("审计总数", str(total_audited), "四维审计覆盖范围", ""),
        ("达标机会", str(len(passed)), f"综合分 ≥ {min_score:.1f}", "sig"),
        ("高优机会", str(len(high)), "综合分 ≥ 8.0", ""),
        ("确认空白", str(confirmed_empty), "检索确认无同类", "gap"),
        ("人工复核", str(review_count), "低证据/临界项", ""),
        ("请求错误", str(request_errors), "检索失败，非空白", ""),
        ("平均综合分", f"{avg:.1f}", "仅统计达标机会", ""),
    ]
    cells = "".join(
        f'<div class="stat"><div class="stat-k">{html.escape(label)}</div>'
        f'<div class="stat-v {cls}">{html.escape(value)}</div>'
        f'<div class="stat-s">{html.escape(sub)}</div></div>'
        for label, value, sub, cls in cards
    )
    st.markdown(f'<div class="statline">{cells}</div>', unsafe_allow_html=True)


def _dim_legend():
    """维度名与配色只在表头出现一次，替代在每一行重复 4 次标签。"""
    items = "".join(
        f'<span><b style="background:{color}"></b>{html.escape(label)}</span>'
        for label, _, color, _ in DIM_META
    )
    return f'<div class="lh-dims">{items}</div>'


def render_ladder(opportunities, min_score):
    rows = _passed(opportunities, min_score)
    if not rows:
        _empty_state("当前门槛下无入选机会", "降低最低分门槛，或先运行一次审计。",
                     "python -m radar audit")
        return

    ctrl1, ctrl2, ctrl3 = st.columns([1.2, 1.4, 2])
    sort_by = ctrl1.selectbox(
        "排序", ["综合分降序", "需求降序", "落差降序", "原生降序", "可行性降序"], index=0
    )
    view = ctrl2.selectbox("过滤", ["全部机会", "高优机会（≥8.0）", "生态空白"], index=0)

    sort_key = {
        "综合分降序": "overall",
        "需求降序": "demand",
        "落差降序": "experience_gap",
        "原生降序": "native_advantage",
        "可行性降序": "indie_feasibility",
    }[sort_by]
    rows = sorted(rows, key=lambda o: float(o["scores"].get(sort_key, 0) or 0), reverse=True)

    if view == "高优机会（≥8.0）":
        rows = [o for o in rows if _overall(o) >= 8.0]
    elif view == "生态空白":
        rows = [o for o in rows if o.get("is_blank")]

    ctrl3.markdown(
        f'<div class="muted" style="padding-top:32px">共 {len(rows)} 条</div>',
        unsafe_allow_html=True,
    )

    if not rows:
        _empty_state("过滤后无结果", "切换过滤条件，或降低门槛。")
        return

    parts = [
        '<div class="ladder"><div class="ladder-head">'
        '<div class="lh-cols"><div>#</div><div>应用</div><div>综合</div><div>定性结论</div></div>'
        + _dim_legend() + '</div>'
    ]
    for i, opp in enumerate(rows, 1):
        s = opp["scores"]
        overall = _overall(opp)
        blank = bool(opp.get("is_blank"))
        pill = '<span class="pill pill-blank">生态空白</span>' if blank else ""
        parts.append(
            f'<div class="ladder-row{" blank" if blank else ""}">'
            f'<div class="lr-head">'
            f'<div class="lr-rank">{i:02d}</div>'
            f'<div class="lr-name">{html.escape(str(opp.get("benchmark_name", "?")))} {pill}</div>'
            f'<div><span class="badge {_score_class(overall)}">'
            f'{overall:.1f}</span></div>'
            f'<div class="lr-verdict">{html.escape(_clip(opp.get("verdict", ""), 68))}</div>'
            f'</div><div class="lr-bars">'
            + "".join(_bar(label, s.get(key, 0), color) for label, key, color, _ in DIM_META)
            + '</div></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_report_section(opp_meta, batch_id: str | None = None):
    st.markdown("---")
    report_path = (RUNS_DIR / batch_id / "report.md") if batch_id else (DATA_DIR / "report.md")
    with st.expander("决策报告预览与下载", expanded=False):
        if not report_path.exists():
            _empty_state("报告未生成", "先运行审计以生成决策报告。", "python -m radar audit")
            return
        try:
            content = report_path.read_text(encoding="utf-8")
        except Exception as exc:
            _empty_state("报告读取失败", f"{exc}")
            return
        if opp_meta:
            st.markdown(
                f'<div class="muted">生成时间 {html.escape(_fmt_ts(opp_meta.get("audited_at", "")))}'
                f' &nbsp; 审计模型 {html.escape(str(opp_meta.get("model") or "—"))}'
                f' &nbsp; 门槛 {opp_meta.get("min_score", "—")}</div>',
                unsafe_allow_html=True,
            )
        st.download_button(
            "下载 report.md",
            data=content,
            file_name=f"report_{batch_id}.md" if batch_id else "report.md",
            mime="text/markdown",
            use_container_width=False,
        )
        st.markdown(content)


# --------------------------------------------------------------------------
# 标签 2 / 3：机会详情与生态空白
# --------------------------------------------------------------------------
def _fact_value(text):
    return html.escape(str(text)) if text not in (None, "") else "—"


def render_deep_card(opp):
    s = opp["scores"]
    overall = _overall(opp)
    blank = bool(opp.get("is_blank"))
    comps = opp.get("competitors") or []
    bad_total = sum(int(c.get("bad_review_count") or 0) for c in comps)
    scores_num = []
    for c in comps:
        try:
            scores_num.append(float(str(c.get("score", "")).strip()))
        except (TypeError, ValueError):
            pass
    avg_score = f"{sum(scores_num) / len(scores_num):.1f}" if scores_num else "—"

    pill = '<span class="pill pill-blank">生态空白</span>' if blank else ""
    grade = opp.get("evidence_grade", "")
    grade_pill = (f'<span class="pill">证据 {html.escape(str(grade))}</span>'
                  if grade else "")
    status = opp.get("probe_status", "")
    status_pill = (f'<span class="pill">状态 {html.escape(str(status))}</span>'
                   if status else "")
    st.markdown(
        f'<div class="detail-head"><span class="detail-name">'
        f'{html.escape(str(opp.get("benchmark_name", "?")))}</span>{pill}'
        f'{grade_pill}{status_pill}'
        f'<span class="badge {_score_class(overall)}">{overall:.1f}</span>'
        f'<span class="muted">by {html.escape(str(opp.get("artist") or "—"))}</span></div>',
        unsafe_allow_html=True,
    )
    if opp.get("jtbd"):
        st.markdown(f'<div class="muted">JTBD · {html.escape(str(opp["jtbd"]))}</div>',
                    unsafe_allow_html=True)
    if opp.get("pick_reason"):
        st.markdown(f'<div class="muted">入选理由 · {html.escape(str(opp["pick_reason"]))}</div>',
                    unsafe_allow_html=True)

    st.markdown(
        '<div class="lr-bars" style="margin-top:14px">'
        + "".join(_bar(label, s.get(key, 0), color) for label, key, color, _ in DIM_META)
        + "</div>",
        unsafe_allow_html=True,
    )

    if opp.get("verdict"):
        st.markdown(f'<div class="verdict" style="margin-top:14px">'
                    f'{html.escape(str(opp["verdict"]))}</div>', unsafe_allow_html=True)

    st.markdown('<div class="panel-title" style="margin-top:16px">事实依据</div>',
                unsafe_allow_html=True)
    if blank:
        st.markdown(
            '<div class="facts">'
            '<div class="fact"><div class="fact-k">鸿蒙同类竞品</div>'
            '<div class="fact-v" style="color:var(--gold)">0 · 生态空白</div></div>'
            '<div class="fact"><div class="fact-k">数据来源</div>'
            '<div class="fact-v" style="font-size:.85rem">华为市场接口</div></div>'
            '</div>'
            '<div class="muted" style="margin-top:8px">未检出真实同类竞品，可直接抢跑。</div>',
            unsafe_allow_html=True,
        )
    else:
        review_total = sum(
            int((c.get("comment_stats") or {}).get("fetched") or 0) for c in comps)
        st.markdown(
            '<div class="facts">'
            f'<div class="fact"><div class="fact-k">同类竞品</div>'
            f'<div class="fact-v">{len(comps)}</div></div>'
            f'<div class="fact"><div class="fact-k">平均竞品评分</div>'
            f'<div class="fact-v">{avg_score}</div></div>'
            f'<div class="fact"><div class="fact-k">差评样本</div>'
            f'<div class="fact-v" style="color:var(--amber)">{bad_total}</div></div>'
            f'<div class="fact"><div class="fact-k">评论采样</div>'
            f'<div class="fact-v">{review_total}</div></div>'
            f'<div class="fact"><div class="fact-k">证据等级</div>'
            f'<div class="fact-v">{_fact_value(grade)}</div></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if comps:
            st.markdown('<div class="panel-title" style="margin-top:16px">竞品事实与差评痛点</div>',
                        unsafe_allow_html=True)
            for c in comps:
                pains = c.get("pain_summary") or []
                pain_html = "".join(
                    f'<div class="pain">· {html.escape(str(p))}</div>' for p in pains
                )
                st.markdown(
                    f'<div class="comp-row"><div class="comp-name">'
                    f'{html.escape(str(c.get("name", "?")))}</div>'
                    f'<div class="comp-meta">'
                    f'<span>评分 {_fact_value(c.get("score"))}</span>'
                    f'<span>下载 {_fact_value(c.get("downloads"))}</span>'
                    f'<span>分类 {_fact_value(c.get("category"))}</span>'
                    f'<span>差评 {int(c.get("bad_review_count") or 0)} 条</span></div>'
                    f'{pain_html}</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown('<div class="muted">未记录竞品明细。</div>', unsafe_allow_html=True)

    feats = opp.get("native_features") or []
    st.markdown('<div class="panel-title" style="margin-top:16px">鸿蒙原生赋能建议</div>',
                unsafe_allow_html=True)
    if feats:
        st.markdown("".join(f"- {html.escape(str(f))}\n" for f in feats))
    else:
        st.markdown('<div class="muted">暂无原生能力建议。</div>', unsafe_allow_html=True)

    if opp.get("attack_vector"):
        st.markdown(f'<div class="panel-title" style="margin-top:16px">MVP 突破切入点</div>'
                    f'<div class="verdict">{html.escape(str(opp["attack_vector"]))}</div>',
                    unsafe_allow_html=True)
    if opp.get("indie_advice"):
        with st.expander("独立开发者行动建议"):
            st.markdown(str(opp["indie_advice"]))


def render_details(opportunities, min_score):
    passed = _passed(opportunities, min_score)
    if not passed:
        _empty_state("当前门槛下无机会可查看", "降低门槛，或先运行一次「仅审计」。")
        return
    options = {
        o.get("apple_id") or o["benchmark_name"]: o
        for o in passed
    }
    labels = {
        key: f"{o['benchmark_name']} · {_overall(o):.1f} 分"
        + ("（生态空白）" if o.get("is_blank") else "")
        for key, o in options.items()
    }
    choice = st.selectbox("选择机会", list(options.keys()), format_func=lambda k: labels[k])
    with st.container(key="detail_card"):
        render_deep_card(options[choice])


def render_review_queue(review_meta, audit_meta):
    """人工复核队列：展示请求错误、低证据与临界项，供人工定夺。"""
    items = (review_meta or {}).get("items", []) if isinstance(review_meta, dict) else []
    if not items:
        _empty_state("当前无需人工复核", "所有审计项证据充分或已达标。",
                     "python -m radar audit")
        return
    st.markdown(
        f'<div class="muted">共 {len(items)} 项待人工复核：请求错误、LLM 过滤失败、'
        f'低证据等级或临界分数。这些项不会被当作生态空白或高潜机会。</div>',
        unsafe_allow_html=True,
    )
    for i, item in enumerate(items):
        with st.container(key=f"review_card_{i}"):
            title = item.get("benchmark_name", "?")
            grade = item.get("evidence_grade", "")
            status = item.get("probe_status", "")
            reasons = "、".join(item.get("reason_codes", []))
            st.markdown(
                f'<div class="muted"><b>{html.escape(str(title))}</b> '
                f'· 证据 {html.escape(str(grade))} · 状态 {html.escape(str(status))} '
                f'· 原因 {html.escape(reasons)}</div>',
                unsafe_allow_html=True,
            )
            for q in item.get("review_questions", []):
                st.caption(q)


def render_ecosystem(opportunities, min_score):
    # 仅接受「确认空白」：排除请求错误与未验证的 LLM 结果
    blanks = sorted(
        [o for o in opportunities
         if o.get("is_blank")
         and o.get("probe_status", "") != "request_error"
         and _overall(o) >= min_score],
        key=_overall,
        reverse=True,
    )
    if not blanks:
        _empty_state("当前门槛下无生态空白机会", "降低门槛，或先运行探测确认供给空缺。",
                     "python -m radar probe")
        return
    st.markdown(
        f'<div class="muted">共 {len(blanks)} 个通过门槛的生态空白机会，未检出真实同类竞品，可直接抢跑。</div>',
        unsafe_allow_html=True,
    )
    for i, opp in enumerate(blanks):
        with st.container(key=f"gap_card_{i}"):
            render_deep_card(opp)


# --------------------------------------------------------------------------
# 标签 4：控制台
# --------------------------------------------------------------------------
def _config_from_form(form):
    return {
        "llm": {
            "base_url": form["base_url"].strip(),
            "api_key": form["api_key"],
            "model": form["model"].strip(),
            "timeout": int(form["timeout"]),
        },
        "capture": {
            "regions": form["regions"],
            "charts": form["charts"],
            "feed_limit": int(form["feed_limit"]),
            "max_benchmarks": int(form["max_benchmarks"]),
            "llm_input_limit": int(form["llm_input_limit"]),
            "rss_retries": int(form["rss_retries"]),
        },
        "probe": {
            "max_comments_per_app": int(form["max_comments_per_app"]),
            "bad_rating_threshold": int(form["bad_rating_threshold"]),
            "max_competitors": int(form["max_competitors"]),
            "max_keywords_per_benchmark": int(form["max_keywords_per_benchmark"]),
            "comment_pages": int(form["comment_pages"]),
            "filter_candidates_limit": int(form["filter_candidates_limit"]),
            "enable_llm_filter": bool(form["enable_llm_filter"]),
        },
        "audit": {"min_score": float(form["min_score"])},
    }


def _render_config_form(cfg):
    llm = cfg.get("llm", {})
    cap = cfg.get("capture", {})
    prb = cfg.get("probe", {})
    aud = cfg.get("audit", {})

    with st.form("config_form"):
        st.markdown("**LLM 推理配置**")
        st.caption("影响 capture / probe / audit 全部阶段。")
        base_url = st.text_input("API 地址", value=llm.get("base_url", ""))
        api_key = st.text_input("API Key", value=llm.get("api_key", ""), type="password")
        model = st.text_input("模型名称", value=llm.get("model", ""))
        timeout = st.number_input("超时（秒）", min_value=10, max_value=1800,
                                  value=int(llm.get("timeout", 120)), step=10)

        st.markdown("---")
        st.markdown("**榜单捕获配置**")
        st.caption("影响 capture 阶段。")
        regions = st.multiselect("地区", ["cn", "us"],
                                 default=cap.get("regions", ["cn", "us"]))
        charts = st.multiselect("榜单", ["top-free", "top-paid"],
                                default=cap.get("charts", ["top-free", "top-paid"]))
        feed_limit = st.number_input("RSS 榜单条数", min_value=1, max_value=200,
                                     value=int(cap.get("feed_limit", 100)), step=10)
        max_benchmarks = st.number_input("精选标杆上限", min_value=1, max_value=100,
                                         value=int(cap.get("max_benchmarks", 20)), step=1)
        llm_input_limit = st.number_input("LLM 输入上限", min_value=1, max_value=500,
                                          value=int(cap.get("llm_input_limit", 200)), step=10)
        rss_retries = st.number_input("RSS 拉取重试次数", min_value=1, max_value=10,
                                      value=int(cap.get("rss_retries", 3)), step=1)

        st.markdown("---")
        st.markdown("**华为探测配置**")
        st.caption("影响 probe 阶段。")
        max_comments_per_app = st.number_input("单应用评价上限", min_value=1, max_value=500,
                                               value=int(prb.get("max_comments_per_app", 50)), step=10)
        bad_rating_threshold = st.number_input("差评星级阈值", min_value=1, max_value=5,
                                               value=int(prb.get("bad_rating_threshold", 3)), step=1)
        max_competitors = st.number_input("单标杆最大竞品数", min_value=1, max_value=20,
                                          value=int(prb.get("max_competitors", 3)), step=1)
        max_keywords_per_benchmark = st.number_input(
            "单标杆最大关键词数", min_value=1, max_value=8,
            value=int(prb.get("max_keywords_per_benchmark", 4)), step=1)
        comment_pages = st.number_input("评论抓取页数", min_value=1, max_value=20,
                                        value=int(prb.get("comment_pages", 4)), step=1)
        filter_candidates_limit = st.number_input("LLM 候选过滤上限", min_value=1, max_value=50,
                                                  value=int(prb.get("filter_candidates_limit", 8)), step=1)
        enable_llm_filter = st.checkbox("启用 LLM 语义去噪", value=bool(prb.get("enable_llm_filter", True)))

        st.markdown("---")
        st.markdown("**审计写入门槛**")
        st.caption("决定 audit 阶段写入 opportunities.json 的最低分；侧栏滑块只改看板展示。")
        min_score = st.number_input("最低分门槛", min_value=0.0, max_value=10.0,
                                    value=float(aud.get("min_score", 6.0)), step=0.5)

        c1, c2 = st.columns(2)
        submitted = c1.form_submit_button("保存配置", use_container_width=True)
        reset = c2.form_submit_button("重置为默认值", use_container_width=True)

    if submitted:
        new_cfg = _config_from_form({
            "base_url": base_url, "api_key": api_key, "model": model, "timeout": timeout,
            "regions": regions, "charts": charts, "feed_limit": feed_limit,
            "max_benchmarks": max_benchmarks, "llm_input_limit": llm_input_limit,
            "rss_retries": rss_retries,
            "max_comments_per_app": max_comments_per_app,
            "bad_rating_threshold": bad_rating_threshold, "max_competitors": max_competitors,
            "max_keywords_per_benchmark": max_keywords_per_benchmark,
            "comment_pages": comment_pages,
            "filter_candidates_limit": filter_candidates_limit,
            "enable_llm_filter": enable_llm_filter, "min_score": min_score,
        })
        errors = validate_config(new_cfg)
        if errors:
            for e in errors:
                st.error(e)
        else:
            save_config(new_cfg)
            st.session_state[MIN_SCORE_KEY] = float(min_score)
            st.toast("配置已保存到 config.toml")
            st.rerun()

    if reset:
        save_config(DEFAULTS)
        st.session_state.pop(MIN_SCORE_KEY, None)
        st.toast("已重置为默认配置")
        st.rerun()


def _render_artifacts(batch_id: str | None = None):
    rows = _artifact_rows(batch_id)
    parts = ['<div class="panel" style="padding:6px 0">']
    for r in rows:
        state = ('<span class="state ok"><span class="dot" aria-hidden="true"></span>已就绪</span>'
                 if r["ok"] else
                 '<span class="state bad"><span class="dot" aria-hidden="true"></span>缺失</span>')
        if r["ok"]:
            count = r["count"] if r["count"] is not None else "—"
            meta = f'{count} 条'
            meta += f'<span style="margin-left:14px">{html.escape(_fmt_ts(r["ts"]))}</span>'
        else:
            meta = html.escape(r["note"])
        parts.append(
            f'<div class="artifact"><div><span class="name">'
            f'{html.escape(r["title"])}</span> <span class="muted">{html.escape(r["file"])}</span></div>'
            f'<div class="meta">{state}<span style="margin-left:14px">{meta}</span></div></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def _render_traces_panel(traces, batch_id):
    """结构化 Traces 检查器：卡片式列表 + 折叠详情。"""
    if batch_id and not traces:
        traces = store.load_traces(batch_id)
    if not traces:
        _empty_state("暂无 LLM 调用追踪",
                     "运行任一阶段后，每次大模型调用的 Prompt 与原始响应会在此展示。",
                     "python -m radar audit")
        return
    ok = sum(1 for t in traces if t.get("status") == "success")
    err = len(traces) - ok
    total_s = sum(float(t.get("duration_s") or 0) for t in traces)
    st.markdown(
        f'<div class="muted">共 {len(traces)} 次调用 · 成功 {ok} · 失败 {err} · '
        f'累计 {total_s:.1f}s</div>',
        unsafe_allow_html=True,
    )
    stage_filter = st.multiselect(
        "按阶段过滤", sorted({str(t.get("stage") or "—") for t in traces}),
        default=sorted({str(t.get("stage") or "—") for t in traces}),
    )
    shown = [t for t in traces if str(t.get("stage") or "—") in stage_filter]
    if not shown:
        st.info("当前过滤条件下无追踪记录。")
        return
    st.markdown("".join(_trace_card(t, i) for i, t in enumerate(shown)),
                unsafe_allow_html=True)


def _render_text_log_panel(logs):
    """纯文本详细模式：终端原味流式日志，支持下载与复制。"""
    text = "\n".join(logs) if logs else "暂无日志"
    st.markdown(
        f'<div class="logbox" role="log" aria-live="polite" aria-label="执行日志">'
        f'{html.escape(text)}</div>',
        unsafe_allow_html=True,
    )
    if logs:
        c1, c2 = st.columns([1, 3])
        c1.download_button("下载日志", data="\n".join(logs), file_name="radar_run.log",
                           mime="text/plain", use_container_width=True)
        with c2.popover("复制日志"):
            st.code("\n".join(logs), language="text")


def render_console(cfg, opportunities):
    st.markdown('<div class="panel-title">数据产物状态</div>', unsafe_allow_html=True)
    _render_artifacts()
    st.caption("所有产物均为 data/ 目录下的原子写入结果；界面读取最近一次成功产物。")

    st.markdown('<div class="panel-title" style="margin-top:18px">流水线执行</div>',
                unsafe_allow_html=True)
    disabled = _is_running()
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("全量扫描", disabled=disabled, use_container_width=True, key="c_full"):
        _start_run("全量扫描", "full")
        st.rerun()
    if c2.button("仅捕获", disabled=disabled, use_container_width=True, key="c_cap"):
        _start_run("仅捕获", "capture")
        st.rerun()
    if c3.button("仅探测", disabled=disabled, use_container_width=True, key="c_prb"):
        _start_run("仅探测", "probe")
        st.rerun()
    if c4.button("仅审计", disabled=disabled, use_container_width=True, key="c_aud"):
        _start_run("仅审计", "audit")
        st.rerun()
    st.caption("全量扫描按 捕获 → 探测 → 审计 顺序执行；网络/LLM 临时故障会自动重试，"
               "仍失败则进入「中断」态，修正配置后可点「继续」从失败步骤恢复。")

    st.markdown('<div class="panel-title" style="margin-top:18px">执行日志</div>',
                unsafe_allow_html=True)
    logs = st.session_state[LOG_KEY]
    traces = st.session_state[TRACES_KEY]
    state = st.session_state[RUN_KEY]
    view = st.radio(
        "视图", ["结构化 Traces 检查器", "纯文本详细模式"],
        horizontal=True, label_visibility="collapsed", key="_log_view",
    )
    if view == "结构化 Traces 检查器":
        _render_traces_panel(traces, state.get("batch_id"))
    else:
        _render_text_log_panel(logs)

    st.markdown('<div class="panel-title" style="margin-top:18px">配置</div>',
                unsafe_allow_html=True)
    _render_config_form(cfg)


# --------------------------------------------------------------------------
# 标签 5：提示词微调
# --------------------------------------------------------------------------
def _group_templates():
    """按阶段分组模板名，供 UI 分区展示。"""
    groups: dict[str, list[str]] = {}
    for name, spec in prompts.PROMPT_TEMPLATES.items():
        groups.setdefault(spec["stage"], []).append(name)
    return groups


def render_prompts():
    st.markdown(
        '<div class="muted">提示词模板独立存放于 <code>prompts/*.txt</code>，'
        '保存后下一次运行立即生效；运行批次会归档当次使用的快照。'
        '「恢复官方默认」会删除自定义文件并回退到内建模板。</div>',
        unsafe_allow_html=True,
    )
    if st.button("导出全部默认模板到 prompts/", use_container_width=False,
                 disabled=_is_running()):
        written = prompts.export_defaults(overwrite=False)
        st.toast(f"已导出 {len(written)} 个默认模板（不覆盖已有自定义文件）")

    groups = _group_templates()
    tabs = st.tabs(list(groups.keys()))
    for tab, (stage, names) in zip(tabs, groups.items()):
        with tab:
            for name in names:
                _render_prompt_editor(name)


def _render_prompt_editor(name: str):
    spec = prompts.PROMPT_TEMPLATES[name]
    current = prompts.get_prompt(name)
    customized = prompts.is_customized(name)
    badge = ('<span class="custom-badge">已自定义</span>' if customized else
             '<span class="muted">内建默认</span>')
    ph = "".join(f'<span class="ph-chip">{{{p}}}</span>' for p in spec["placeholders"])
    st.markdown(
        f'<div class="prompt-head"><span class="prompt-name">{html.escape(spec["label"])}</span>'
        f'<span class="muted">{html.escape(name)}.txt</span>{badge}</div>'
        + (f'<div style="margin-bottom:6px">必要占位符: {ph}</div>' if spec["placeholders"] else ""),
        unsafe_allow_html=True,
    )
    widget_key = f"prompt_edit_{name}"
    sync_key = f"{widget_key}_sync"
    # 外部改动（保存/重置）后同步编辑器内容：必须在 text_area 实例化之前完成
    if st.session_state.get(sync_key) != current:
        st.session_state[widget_key] = current
        st.session_state[sync_key] = current
    st.session_state.setdefault(widget_key, current)
    text = st.text_area("模板内容", key=widget_key, height=200,
                        label_visibility="collapsed")
    c1, c2 = st.columns([1, 1])
    save_clicked = c1.button("保存模板", key=f"save_{name}", use_container_width=True,
                             disabled=_is_running())
    reset_clicked = c2.button("恢复官方默认", key=f"reset_{name}", use_container_width=True,
                              disabled=_is_running())
    if save_clicked:
        errors = prompts.validate_prompt(name, text)
        if errors:
            for e in errors:
                st.error(e)
        else:
            prompts.save_prompt(name, text)
            st.session_state[sync_key] = text
            st.toast(f"{spec['label']} 已保存")
            st.rerun()
    if reset_clicked:
        prompts.reset_prompt(name)
        st.session_state.pop(sync_key, None)
        st.toast(f"{spec['label']} 已恢复默认")
        st.rerun()
    st.markdown("---")


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def main():
    _init_session()
    _drain_queue()

    cfg = load_config()
    min_score = sidebar_panel(cfg)

    with st.container(key="history_bar"):
        batch_id = render_history_selector()

    feed, bench, probe, opps, audits, review, errors = _load_all(batch_id)
    opportunities = (opps or {}).get("opportunities", []) if isinstance(opps, dict) else []
    opp_meta = opps if isinstance(opps, dict) else None
    audit_meta = audits if isinstance(audits, dict) else None
    review_meta = review if isinstance(review, dict) else None

    for err in errors:
        st.error(err)
    if batch_id:
        st.markdown(
            f'<div class="muted">正在回溯历史批次 <b>{html.escape(batch_id)}</b>；'
            f'「最新」可返回 data/ 顶层镜像。</div>',
            unsafe_allow_html=True,
        )

    render_brand(st.session_state[RUN_KEY], opp_meta, min_score, opportunities)
    render_run_strip()
    st.markdown("---")

    t1, t2, t3, t_review, t4, t5 = st.tabs(
        ["机会总览", "机会详情", "生态空白", "人工复核", "提示词微调", "控制台"])

    with t1:
        if opps is None:
            _empty_state("尚未审计", "先完成一次审计，或在侧栏触发全量扫描。",
                         "python -m radar audit")
        elif not opportunities:
            _empty_state("已审计但无机会记录", "审计结果为空，检查前序产物后重新运行审计。",
                         "python -m radar audit")
        else:
            render_kpi_row(opportunities, min_score, audit_meta, review_meta, probe)
            st.markdown("")
            render_ladder(opportunities, min_score)
            render_report_section(opp_meta, batch_id)

    with t2:
        if not opportunities:
            _empty_state("暂无可查看的机会", "先完成一次审计，或在总览页调整门槛。",
                         "python -m radar audit")
        else:
            render_details(opportunities, min_score)

    with t3:
        if not opportunities:
            _empty_state("暂无可查看的生态空白", "先完成一次审计。", "python -m radar audit")
        else:
            render_ecosystem(opportunities, min_score)

    with t_review:
        render_review_queue(review_meta, audit_meta)

    with t4:
        render_prompts()

    with t5:
        render_console(cfg, opportunities)

    st.sidebar.markdown("---")
    st.sidebar.caption("Harmony Opportunity Radar · M4 情报看板")


if __name__ == "__main__":
    main()