# -*- coding: utf-8 -*-
"""
M0 PoC: SearXNG 检索可达性验证（用户自建服务，零成本）
用法: python poc/test_searxng.py http://你的searxng地址 [API_KEY(若有)]
前提: SearXNG 实例需开启 JSON 输出（settings.yml 中 search.formats 含 json）
目的: 验证通过自建 SearXNG 做 site: 定向检索，间接探测华为应用市场供给。
输出: poc/out/searxng_findings.json —— 检索结果样例
"""
import json
import sys
from pathlib import Path

import httpx

if len(sys.argv) < 2:
    print("用法: python poc/test_searxng.py http://你的searxng地址 [API_KEY]")
    sys.exit(1)

BASE = sys.argv[1].rstrip("/")
API_KEY = sys.argv[2] if len(sys.argv) > 2 else None

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# 三类查询分别验证: 站内定向 / 站内关键词 / 泛搜索
QUERIES = [
    "site:appgallery.huawei.com 倒计时",
    "site:appgallery.huawei.com 番茄钟",
    "华为应用市场 倒计时 应用",
]

headers = {}
if API_KEY:
    headers["Authorization"] = f"Bearer {API_KEY}"

findings = {}
with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as c:
    for q in QUERIES:
        try:
            r = c.get(f"{BASE}/search", params={"q": q, "format": "json"})
            if r.status_code == 200:
                data = r.json()
                results = data.get("results", [])
                findings[q] = {
                    "status": 200,
                    "count": len(results),
                    "sample": [
                        {"title": x.get("title"), "url": x.get("url"), "engine": x.get("engine")}
                        for x in results[:5]
                    ],
                }
                print(f"[OK] {q} -> {len(results)} 条结果")
                for x in results[:3]:
                    print(f"     - {x.get('title', '')[:50]} | {x.get('url', '')[:80]}")
            else:
                findings[q] = {"status": r.status_code, "body_head": r.text[:200]}
                print(f"[FAIL] {q} -> HTTP {r.status_code}: {r.text[:150]}")
        except Exception as e:
            findings[q] = {"error": f"{type(e).__name__}: {e}"}
            print(f"[FAIL] {q} -> {e}")

(OUT / "searxng_findings.json").write_text(
    json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\n结果已保存: {OUT / 'searxng_findings.json'}")
