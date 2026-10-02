# -*- coding: utf-8 -*-
"""
M0 PoC 第九轮: 评论接口实测（SPEC「差评挖掘」的最后一块拼图）
线索: webpack 分块中挖到 '/commentList/' 与 '/index/commentlist3' 两个路径。
本轮: 用已打通的 interface-code 流程穷举 commentlist3 的调用形态。
输出: poc/out/comments_verified.json
"""
import json
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

API_BASE = "https://web-drcn.hispace.dbankcloud.com/edge"
WEB = "https://appgallery.huawei.com"
APPID = "C103571573"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": WEB + "/",
    "Origin": WEB,
}

results = []
with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
    # interface code
    r1 = c.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
    code = r1.text.strip().strip('"')
    ic = f"{code}_{int(time.time() * 1000)}"

    # 候选 1: POST /index/commentlist3，body 含 appid 与分页
    cands = [
        ("POST", f"{API_BASE}/index/commentlist3",
         {"appid": APPID, "pageNum": 1, "pageSize": 10}),
        ("POST", f"{API_BASE}/index/commentlist3",
         {"appid": APPID, "reqPageNum": 1, "maxResults": 10}),
        ("GET", f"{API_BASE}/index/commentlist3",
         {"appid": APPID, "serviceType": 20, "reqPageNum": 1, "maxResults": 10,
          "version": "10.0.0", "zone": "", "locale": "zh"}),
    ]
    for method, url, payload in cands:
        try:
            if method == "POST":
                r = c.post(url, json=payload, headers={"interface-code": ic})
            else:
                r = c.get(url, params=payload, headers={"interface-code": ic})
            head = r.text[:250].replace("\n", " ")
            ok = r.status_code == 200 and "rtnCode" in r.text and ('"rtnCode":0' in r.text.replace(" ", "").replace('"rtnCode":', '"rtnCode":'))
            # rtnCode 0 判断（宽松匹配两种格式）
            rtn0 = '"rtnCode":0' in r.text.replace(" ", "") or '"rtnCode" : 0' in r.text
            results.append({"method": method, "url": url, "payload": payload,
                            "status": r.status_code, "rtn0": rtn0, "body_head": head})
            print(f"[{method} {url.split('/edge')[1]}] params={list(payload.keys())}")
            print(f"   HTTP {r.status_code} rtn0={rtn0} | {head[:160]}")
            if rtn0:
                d = r.json()
                print("\n✓✓✓ 评论接口打通！结构分析:")
                (OUT / "comments_verified.json").write_text(
                    json.dumps({"request": {"method": method, "url": url, "payload": payload},
                                "response": d}, ensure_ascii=False, indent=2), encoding="utf-8")
                # 递归打印数组结构
                def walk(obj, path="root", depth=0):
                    if depth > 7:
                        return
                    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                        print(f"  数组 {path}: {len(obj)} 条")
                        for k, v in obj[0].items():
                            print(f"    {k}: {str(v)[:60]}")
                    elif isinstance(obj, dict):
                        for k, v in obj.items():
                            walk(v, f"{path}.{k}", depth + 1)
                walk(d)
                print("\n完整响应已保存 comments_verified.json")
                break
        except Exception as e:
            results.append({"method": method, "url": url, "error": str(e)})
            print(f"[FAIL] {method} {url}: {e}")

if not any(x.get("rtn0") for x in results):
    # 保存失败现场供下一步分析
    (OUT / "comments_verified.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n本轮未打通，失败现场已保存，需回 JS 挖 commentlist3 的真实参数结构")
