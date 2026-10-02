# -*- coding: utf-8 -*-
"""
M0 PoC 第七轮（决胜局）: 完整复现 interface-code 三步流程
JS 逆向还原的机制（来自 7.eaee8b...js 的拦截器代码）:
  1. POST {API_BASE}/webedge/getInterfaceCode  body {} → 返回 code，存 sessionStorage
  2. 业务请求拦截器: 当 sysConfig.interfaceCodeSwitch == "on" 时注入
     header "interface-code": "{code}_{当前毫秒时间戳}"
  3. 响应拦截器: 若 rtnCode == 1002 → 刷新 code 后自动重试
验证: 走完三步后调搜索 API，能拿到应用列表 → 逆向路线正式宣告打通。
输出: poc/out/full_flow_verified.json
"""
import json
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

API_BASE = "https://web-drcn.hispace.dbankcloud.com/edge"

UA = {
    "User-Agent": "Mozilla/5.0Windows NT 10.0; Win64; x64".replace("5.0Windows", "5.0 (Windows"),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://appgallery.huawei.com/",
    "Origin": "https://appgallery.huawei.com",
}

SEARCH_PARAMS = {
    "method": "internal.getTabDetail",
    "serviceType": 20,
    "reqPageNum": 1,
    "uri": "searchApp|倒计时",
    "maxResults": 25,
    "version": "10.0.0",
    "zone": "",
    "locale": "zh",
}

with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
    # ---- 第 1 步: 获取 interface code ----
    r1 = c.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
    print(f"[1] getInterfaceCode: HTTP {r1.status_code}")
    print(f"    响应: {r1.text[:200]}")
    code = r1.text.strip().strip('"') if r1.status_code == 200 else None
    # 有些实现返回纯文本 code，有些返回 JSON；两种都兼容
    try:
        j = r1.json()
        code = j.get("data") if isinstance(j, dict) else j
    except Exception:
        pass
    print(f"    提取到 code: {str(code)[:60]}")

    if not code:
        print("!! 未拿到 code，流程终止")
        raise SystemExit(1)

    # ---- 第 2 步: 带 interface-code 头调搜索 API ----
    ic_header = f"{code}_{int(time.time() * 1000)}"
    r2 = c.get(
        f"{API_BASE}/uowap/index",
        params=SEARCH_PARAMS,
        headers={"interface-code": ic_header},
    )
    print(f"\n[2] 搜索 API: HTTP {r2.status_code}")
    print(f"    响应前 300 字: {r2.text[:300]}")

    findings = {
        "interface_code": str(code)[:80],
        "interface_code_header": ic_header,
        "search_status": r2.status_code,
        "search_body_head": r2.text[:1000],
    }

    if r2.status_code == 200 and '"rtnCode":0' in r2.text.replace(" ", ""):
        print("\n✓✓✓ 搜索接口打通！返回结构分析:")
        d = r2.json()
        # 递归找应用列表数组
        def walk(obj, path="root", depth=0):
            if depth > 7:
                return
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                ks = set(obj[0].keys())
                if ks & {"name", "appName", "packageName", "pkgName", "appid", "intro", "rateScore"}:
                    print(f"  数组 {path}: {len(obj)} 条")
                    first = obj[0]
                    for k in sorted(ks):
                        v = str(first.get(k, ""))[:60]
                        print(f"    {k}: {v}")
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{path}.{k}", depth + 1)

        walk(d)
        findings["full_response"] = d

    (OUT / "full_flow_verified.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已保存: {OUT / 'full_flow_verified.json'}")
