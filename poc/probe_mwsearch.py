# -*- coding: utf-8 -*-
"""
M0 PoC 第二轮: 深挖 /mwsearch 等关键接口的调用方式
思路: 上一轮从 JS bundle 里挖出了 22 个候选路径，但不知道请求方法和参数结构。
      本轮抓取 JS 源码中这些接口附近的代码上下文（前后各 400 字符），
      从中还原出 fetch/axios 调用的真实形态（method / url / body 字段名）。
输出: poc/out/api_contexts.json —— 每个关键接口的代码上下文
"""
import json
import re
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

BASE = "https://appgallery.huawei.com"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 重点关注的接口关键词（第一轮发现的高价值候选）
KEYS = ["mwsearch", "getnewhotsearchlist", "appinfo/query", "webedge/appinfo", "app_simple"]

client = httpx.Client(headers=UA, timeout=20, follow_redirects=True)

# 重新拉首页拿 bundle 列表
r = client.get(BASE + "/")
scripts = re.findall(r'src="([^"]+\.js[^"]*)"', r.text)
urls = []
for s in scripts:
    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("/"):
        s = BASE + s
    elif not s.startswith("http"):
        continue
    urls.append(s)

contexts = {k: [] for k in KEYS}
for u in urls:
    try:
        rj = client.get(u)
        if rj.status_code != 200:
            continue
        text = rj.text
        for k in KEYS:
            # 抓每个关键词前 400 / 后 400 字符的上下文，最多 3 处
            for m in list(re.finditer(re.escape(k), text))[:3]:
                start = max(0, m.start() - 400)
                end = min(len(text), m.end() + 400)
                ctx = text[start:end]
                # 只保留包含调用特征的上下文（url/method/params/post 等），过滤纯字符串表
                if any(sig in ctx for sig in ("url", "method", "post", "get", "params", "request")):
                    contexts[k].append({"bundle": u.split("/")[-1], "context": ctx})
    except Exception as e:
        print(f"    ! {u} 失败: {e}")

# 控制台输出摘要：每类上下文里 url/method 附近的紧凑片段
for k, items in contexts.items():
    print(f"\n===== {k}: {len(items)} 段有效上下文 =====")
    for it in items[:2]:
        # 压缩空白便于阅读
        compact = re.sub(r"\s+", " ", it["context"])
        print(f"--- [{it['bundle']}] ---")
        print(compact[:700])

(OUT / "api_contexts.json").write_text(
    json.dumps(contexts, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\n完整上下文已保存: {OUT / 'api_contexts.json'}")
