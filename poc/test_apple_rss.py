# -*- coding: utf-8 -*-
"""
M0 PoC: Apple 官方 RSS 榜单可达性验证
目的: 确认 Stage 1「标杆捕获」的 iOS 侧数据源稳定可用（官方公开接口，无需鉴权）。
输出: poc/out/apple_rss_sample.json —— 中国区/美国区 付费榜/免费榜样例数据
结论判据: 全部矩阵返回 200 且有应用列表 → iOS 侧数据源判定为「稳定可用」
"""
import json
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# 测试矩阵: 国家 × 榜单类型（SPEC 要求付费榜 + 精选/热门榜，这里覆盖付费/免费两种）
MATRIX = [
    ("cn", "top-paid", 25),
    ("cn", "top-free", 25),
    ("us", "top-paid", 25),
]

results = {}
with httpx.Client(timeout=20, follow_redirects=True) as c:
    for cc, chart, n in MATRIX:
        # 官方接口格式: /api/v2/{国家}/apps/{榜单类型}/{数量}/apps.json
        url = f"https://rss.applemarketingtools.com/api/v2/{cc}/apps/{chart}/{n}/apps.json"
        try:
            r = c.get(url)
            ok = r.status_code == 200
            apps = r.json()["feed"]["results"] if ok else []
            results[f"{cc}/{chart}"] = {
                "status": r.status_code,
                "count": len(apps),
                # 只留前 5 条做样例，完整数据 M1 阶段再拉
                "sample": [
                    {"name": a["name"], "artist": a["artistName"], "id": a.get("id"), "url": a.get("url")}
                    for a in apps[:5]
                ],
            }
            print(f"[{'OK' if ok else 'FAIL'}] {cc}/{chart}: HTTP {r.status_code}, {len(apps)} apps")
            for a in apps[:3]:
                print(f"       - {a['name']} | {a['artistName']}")
        except Exception as e:
            # 网络异常也记录下来，PoC 要的是「事实」，无论好坏
            results[f"{cc}/{chart}"] = {"error": f"{type(e).__name__}: {e}"}
            print(f"[FAIL] {cc}/{chart}: {e}")

(OUT / "apple_rss_sample.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\n样例已保存: {OUT / 'apple_rss_sample.json'}")
