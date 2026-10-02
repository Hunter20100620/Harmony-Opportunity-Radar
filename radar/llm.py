# -*- coding: utf-8 -*-
"""LLM 客户端: OpenAI 兼容协议直连（httpx 实现，不依赖 openai SDK）。

SPEC 原则「模型自由度」: 分析引擎必须解耦，本地 vLLM 与商用中转
都走同一个 /chat/completions 协议，换模型只改 config.toml 三个字段。

M4 可观测性: 每次调用都会记录一条结构化 trace
（阶段、标杆、耗时、System/User Prompt、原始响应、解析结果、错误），
写入当前批次的 llm_traces.jsonl，并通过 telemetry 广播给 WebUI。
trace 记录失败绝不影响主流程。
"""
import json
import re
import time

import httpx


class LLMError(Exception):
    """LLM 调用失败（网络/鉴权/响应格式）的统一异常。"""


class LLMClient:
    def __init__(self, llm_cfg: dict, stage: str = "", target: str = ""):
        self.base_url = llm_cfg["base_url"].rstrip("/")
        self.api_key = llm_cfg.get("api_key", "EMPTY")
        self.model = llm_cfg["model"]
        self.timeout = llm_cfg.get("timeout", 120)
        # 默认 trace 上下文（可在单次调用中覆盖）
        self.stage = stage
        self.target = target

    def chat(self, system: str, user: str, temperature: float = 0.1, *,
             stage: str | None = None, target: str | None = None,
             record: bool = True) -> str:
        """发一轮对话，返回纯文本回复。temperature 默认压低——
        精选/审计任务要的是稳定判断，不是创作发挥。"""
        started = time.time()
        raw = None
        error = None
        try:
            raw = self._request(system, user, temperature)
            return raw
        except LLMError as e:
            error = str(e)
            raise
        finally:
            if record:
                self._record_trace(
                    stage if stage is not None else self.stage,
                    target if target is not None else self.target,
                    system, user, raw, None, error, time.time() - started,
                )

    def chat_json(self, system: str, user: str, temperature: float = 0.1, *,
                  stage: str | None = None, target: str | None = None,
                  record: bool = True):
        """chat() 的 JSON 强化版：从回复中稳健地抠出 JSON 数组/对象。

        本地小模型经常不守规矩——会加 ```json 围栏、前后寒暄、
        尾部逗号等，这里全部兜住。返回解析后的 Python 对象。
        """
        started = time.time()
        raw = None
        parsed = None
        error = None
        try:
            raw = self._request(system, user, temperature)
            parsed = extract_json(raw)
            return parsed
        except LLMError as e:
            error = str(e)
            raise
        finally:
            if record:
                self._record_trace(
                    stage if stage is not None else self.stage,
                    target if target is not None else self.target,
                    system, user, raw, parsed, error, time.time() - started,
                )

    def _request(self, system: str, user: str, temperature: float) -> str:
        """底层 HTTP 调用，返回纯文本回复；失败抛 LLMError。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": 65536,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
                # 直连 LLM 服务，忽略系统代理：本地代理常对中转域名 TLS 握手失败
                # （[SSL: UNEXPECTED_EOF_WHILE_READING]），与 apple_rss.py 保持一致。
                trust_env=False,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"无法连接 LLM 服务 ({self.base_url}): {e}") from e

        if resp.status_code != 200:
            raise LLMError(
                f"LLM 返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"LLM 响应结构异常: {resp.text[:300]}") from e

    # ---- 调用追踪 ----
    def _record_trace(self, stage, target, system, user, raw, parsed, error, duration_s):
        """记录一条 trace：落盘到当前批次 + 广播 telemetry 事件（永不抛错）。"""
        try:
            trace = {
                "timestamp": _now(),
                "stage": stage or "",
                "target_name": target or "",
                "duration_s": round(float(duration_s), 2),
                "status": "success" if error is None else "error",
                "model": self.model,
                "base_url": self.base_url,
                "system_prompt": system,
                "user_prompt": user,
                "raw_response": raw,
                "parsed_data": parsed,
                "error": error,
            }
            # 落盘到当前批次
            try:
                from radar import store
                batch = store.active_batch()
                if batch is not None:
                    batch.append_trace(trace)
            except Exception:
                pass
            # 广播给前端
            try:
                from radar import telemetry
                telemetry.trace({
                    "stage": trace["stage"],
                    "target_name": trace["target_name"],
                    "duration_s": trace["duration_s"],
                    "status": trace["status"],
                    "model": trace["model"],
                    "system_prompt": system,
                    "user_prompt": user,
                    "raw_response": raw,
                    "parsed_data": parsed,
                    "error": error,
                })
            except Exception:
                pass
        except Exception:
            pass


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def extract_json(text: str):
    """从 LLM 回复文本中提取 JSON。

    策略（按优先级）:
      1. 剥掉 ```json ... ``` 围栏后整体解析
      2. 找第一个 '[' 到最后一个 ']' 的片段解析（数组场景）
      3. 找第一个 '{' 到最后一个 '}' 的片段解析（对象场景）
    全部失败则抛 LLMError。
    """
    # 去围栏：兼容 ```json、```JSON、``` 等
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S | re.I)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text.strip())

    # 加上括号切片候选
    if "[" in text and "]" in text:
        candidates.append(text[text.index("["): text.rindex("]") + 1])
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])

    for cand in candidates:
        try:
            return json.loads(cand)
        except (ValueError, json.JSONDecodeError):
            continue
    raise LLMError(f"无法从 LLM 回复中解析出 JSON，前 300 字: {text[:300]}")