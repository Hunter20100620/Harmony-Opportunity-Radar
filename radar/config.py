# -*- coding: utf-8 -*-
"""配置加载: 读取/写入项目根目录的 config.toml，缺失时回退到内置默认值。

设计原则（SPEC「轻量化」）: 不引入 pydantic 等重型校验框架，
普通 dict + 明确的默认值即可满足流水线需求。
"""
import os
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path

# 项目根目录 = radar/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"  # 数据总线：所有阶段的 JSON 都落在这里

# 内置默认值：config.toml 缺失或字段缺失时使用，保证脚本永远能跑
DEFAULTS = {
    "llm": {
        "base_url": "http://localhost:8000/v1",
        "api_key": "EMPTY",
        "model": "qwen2.5-coder-32b",
        "timeout": 120,
    },
    "capture": {
        "regions": ["cn", "us"],
        "charts": ["top-free", "top-paid"],
        "feed_limit": 100,
        "max_benchmarks": 20,
        "llm_input_limit": 200,
        "rss_retries": 3,
    },
    "probe": {
        "max_comments_per_app": 50,
        "bad_rating_threshold": 3,
        "max_competitors": 3,
        "max_keywords_per_benchmark": 4,
        "comment_pages": 4,
        "filter_candidates_limit": 8,
        "enable_llm_filter": True,
    },
    "audit": {
        "min_score": 6.0,
    },
}


def load_config() -> dict:
    """读取 config.toml（浅合并进默认值），返回配置 dict。

    注意是「逐段、逐键」合并而非整体替换，这样用户的 config.toml
    只需要写想覆盖的字段，其余自动继承默认值。
    """
    cfg_path = PROJECT_ROOT / "config.toml"
    merged = {section: dict(vals) for section, vals in DEFAULTS.items()}

    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            user_cfg = tomllib.load(f)
        for section, vals in user_cfg.items():
            if isinstance(vals, dict):
                merged.setdefault(section, {}).update(vals)
            else:
                merged[section] = vals  # 顶层标量字段直接覆盖

    return merged


def _check_number_range(label: str, value, lo, hi) -> list[str]:
    """轻量数值范围校验：返回错误信息列表（空列表代表通过）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"{label}必须是数字。"]
    if value < lo or value > hi:
        return [f"{label}必须在 {lo}–{hi} 之间（当前 {value}）。"]
    return []


def validate_config(cfg: dict) -> list[str]:
    """对配置做轻量格式/范围校验，返回错误信息列表。

    只校验格式与取值范围，不探测网络可达性、不校验 API Key 有效性
    （允许离线模型服务、占位 Key）。空列表代表配置合法。
    """
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["配置内容必须是字典结构。"]

    llm = cfg.get("llm") or {}
    base_url = str(llm.get("base_url", "") or "").strip()
    if not base_url:
        errors.append("LLM 配置：API 地址不能为空。")
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors.append("LLM 配置：API 地址必须以 http:// 或 https:// 开头。")
    if not str(llm.get("model", "") or "").strip():
        errors.append("LLM 配置：模型名称不能为空。")
    errors += _check_number_range("LLM 配置：超时(秒)", llm.get("timeout"), 10, 1800)

    cap = cfg.get("capture") or {}
    regions = cap.get("regions")
    if not isinstance(regions, list) or not regions:
        errors.append("捕获配置：至少选择一个地区。")
    charts = cap.get("charts")
    if not isinstance(charts, list) or not charts:
        errors.append("捕获配置：至少选择一个榜单。")
    errors += _check_number_range("捕获配置：RSS 榜单条数", cap.get("feed_limit"), 1, 200)
    errors += _check_number_range("捕获配置：精选标杆上限", cap.get("max_benchmarks"), 1, 100)
    errors += _check_number_range("捕获配置：LLM 输入上限", cap.get("llm_input_limit"), 1, 500)
    errors += _check_number_range("捕获配置：RSS 拉取重试次数", cap.get("rss_retries"), 1, 10)

    prb = cfg.get("probe") or {}
    errors += _check_number_range("探测配置：单应用评价上限", prb.get("max_comments_per_app"), 1, 500)
    errors += _check_number_range("探测配置：差评星级阈值", prb.get("bad_rating_threshold"), 1, 5)
    errors += _check_number_range("探测配置：单标杆最大竞品数", prb.get("max_competitors"), 1, 20)
    errors += _check_number_range("探测配置：单标杆最大关键词数", prb.get("max_keywords_per_benchmark"), 1, 8)
    errors += _check_number_range("探测配置：评论抓取页数", prb.get("comment_pages"), 1, 20)
    errors += _check_number_range("探测配置：LLM 候选过滤上限", prb.get("filter_candidates_limit"), 1, 50)

    audit = cfg.get("audit") or {}
    min_score = audit.get("min_score")
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        errors.append("审计配置：最低分门槛必须是数字。")
    elif not (0.0 <= float(min_score) <= 10.0):
        errors.append("审计配置：最低分门槛必须在 0–10 之间。")

    return errors


def _toml_value(v):
    """将 Python 值序列化为 TOML 合法文本（仅覆盖 config 用到的类型）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        # 简单字符串不加引号也没歧义的可以裸写，否则双引号
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    # int / float
    return str(v)


def reset_prompts_to_default() -> list[str]:
    """恢复全部提示词模板为内建默认（删除 prompts/*.txt 用户自定义文件）。

    延迟导入 radar.prompts 以避免与 prompts 模块的循环依赖。
    返回仍保留默认内容的模板名列表。
    """
    from radar import prompts
    prompts.reset_all_prompts()
    return list(prompts.PROMPT_TEMPLATES.keys())


def export_default_prompts(overwrite: bool = False) -> list[str]:
    """把内建默认提示词导出为 prompts/*.txt 文件，返回写入路径列表。"""
    from radar import prompts
    return prompts.export_defaults(overwrite=overwrite)


def save_config(cfg: dict) -> Path:
    """将配置字典原子写入 config.toml，保留可读格式与分区注释。"""
    cfg_path = PROJECT_ROOT / "config.toml"

    ordered_sections = [
        ("llm", "LLM 推理配置"),
        ("capture", "榜单捕获配置"),
        ("probe", "华为探测与评价抓取配置"),
        ("audit", "审计门槛配置"),
    ]

    lines = [
        f"# 由 Harmony Opportunity Radar WebUI 自动写入 ({datetime.now():%Y-%m-%d %H:%M})",
        "# 手动编辑同样生效，下次保存时会被覆盖。",
        "",
    ]

    for section, comment in ordered_sections:
        vals = cfg.get(section)
        if not isinstance(vals, dict):
            continue
        lines.append(f"# {comment}")
        lines.append(f"[{section}]")
        for key, val in vals.items():
            lines.append(f"{key} = {_toml_value(val)}")
        lines.append("")

    text = "\n".join(lines)

    fd, tmp = tempfile.mkstemp(suffix=".toml", prefix="config_", dir=str(PROJECT_ROOT))
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)

    os.replace(tmp, cfg_path)
    return cfg_path
