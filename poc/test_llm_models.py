# -*- coding: utf-8 -*-
"""M1 辅助: 快速测试 LM Studio 里哪些模型能真正加载并响应。
用法: python poc/test_llm_models.py [model_id1 model_id2 ...]
不传参数则测 LM Studio 当前已加载的模型（从 /api/v0/models 读取 state=loaded）。
"""
import sys
import httpx

BASE = "http://localhost:1234/v1"

# 手动指定时的优先列表（LM Studio GUI 场景备用）
PREFERRED = [
    "qwen3.5-9b",
    "gemma-4-31b-it",
    "qwen2.5-coder-14b-instruct",
]


def get_loaded_models():
    """读 LM Studio 原生接口，返回 state=loaded 的模型 id 列表。
    OpenAI 兼容端点的 model 名与原生 id 一致（新版 LM Studio）。"""
    try:
        r = httpx.get("http://localhost:1234/api/v0/models", timeout=10)
        data = r.json().get("data", [])
        return [m["id"] for m in data if m.get("state") == "loaded"]
    except Exception as e:
        print(f"[warn] 读取原生接口失败: {e}")
        return []


def test_model(model: str) -> bool:
    """发一个最小请求，模型能返回即视为可用。"""
    try:
        r = httpx.post(
            f"{BASE}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "回复一个字: 好"}],
                "max_tokens": 10,
                "temperature": 0,
            },
            timeout=300,
        )
        ok = r.status_code == 200 and "choices" in r.text
        print(f"  {'OK ' if ok else 'FAIL'} {model}"
              + ("" if ok else f" | {r.text[:150]}"))
        return ok
    except Exception as e:
        print(f"  FAIL {model} | {e}")
        return False


if __name__ == "__main__":
    names = sys.argv[1:] or get_loaded_models() or PREFERRED
    print(f"测试 {len(names)} 个模型...")
    ok_list = [m for m in names if test_model(m)]
    print("\n可用模型:", ok_list or "无")
