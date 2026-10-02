# -*- coding: utf-8 -*-
"""
M0 PoC 第五轮: 纯 httpx 复现浏览器捕获的搜索接口（决定性验证）
接口（浏览器网络层捕获的事实）:
  GET https://web-drcn.hispace.dbankcloud.com/edge/uowap/index
  参数: method=internal.getTabDetail, serviceType=20, reqPageNum=1,
        uri=searchApp|{关键词}, maxResults=25, version=10.0.0, zone=, locale=zh
判据: 匿名、无 cookie 的 httpx 请求能返回应用列表 JSON → 逆向路线正式打通。
输出: poc/out/search_api_verified.json —— 复现结果与响应结构分析
"""
import json
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

API = "https://web-drcn.hispace.dbankcloud.com/edge/uowap/index"

# 完全复刻浏览器观察到的参数形态（zone 留空 = 中国区）
params = {
    "method": "internal.getTabDetail",
    "serviceType": 20,
    "reqPageNum": 1,
    "uri": "searchApp|倒计时",
    "maxResults": 25,
    "version": "10.0.0",
    "zone": "",
    "locale": "zh",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://appgallery.huawei.com/",
    "Origin": "https://appgallery.huawei.com",
}

summary = {}
with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as c:
    r = c.get(API, params=params)
    print(f"HTTP {r.status_code}, {len(r.text)} bytes")
    summary["status"] = r.status_code

    if r.status_code == 200:
        try:
            d = r.json()
        except Exception as e:
            print(f"JSON 解析失败: {e}")
            print(r.text[:500])
            raise SystemExit(1)

        rtn = d.get("rtnCode")
        print(f"rtnCode: {rtn}")
        summary["rtnCode"] = rtn

        # 递归找结果数组（响应结构未知，自动探路）
        def find_lists(obj, path="root", depth=0):
            """递归找出所有「元素为 dict 且看起来像应用条目」的数组"""
            found = []
            if depth > 6:
                return found
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                keys = set(obj[0].keys())
                # 应用条目通常有名字/包名/评分类字段
                if keys & {"name", "appName", "packageName", "pkgName", "appid", "appId", "intro", "rateScore"}:
                    found.append((path, len(obj), sorted(keys)[:25]))
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    found.extend(find_lists(v, f"{path}.{k}", depth + 1))
            return found

        lists = find_lists(d)
        summary["result_arrays"] = [
            {"path": p, "count": n, "keys": ks} for p, n, ks in lists
        ]
        for p, n, ks in lists:
            print(f"\n找到结果数组: {p} ({n} 条)")
            print(f"  字段: {ks}")
            # 打印第一条的关键信息
            node = d
            for seg in p.split(".")[1:]:
                if isinstance(node, list):
                    seg = int(seg) if seg.isdigit() else seg
                node = node[seg] if seg in (node if isinstance(node, dict) else range(len(node))) else node
            # 简化: 直接重新定位第一条
            node = d
            ok = True
            for seg in p.split(".")[1:]:
                try:
                    if isinstance(node, list):
                        node = node[0] if isinstance(node[0], dict) and seg in node[0] else node
                    if isinstance(node, dict) and seg in node:
                        node = node[seg]
                    else:
                        ok = False
                        break
                except Exception:
                    ok = False
                    break
            if ok and isinstance(node, list) and node:
                first = node[0]
                print("  第一条样例:")
                for k in ("name", "appName", "packageName", "pkgName", "appid", "appId", "intro", "rateScore", "ratio", "downs", "downloaded"):
                    if k in first:
                        print(f"    {k}: {str(first[k])[:80]}")

        # 保存完整响应供后续分析
        (OUT / "search_api_verified.json").write_text(
            json.dumps({"request": {"url": API, "params": params},
                        "summary": summary,
                        "raw_response_head": d if rtn == 0 else r.text[:2000]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已保存: {OUT / 'search_api_verified.json'}")
