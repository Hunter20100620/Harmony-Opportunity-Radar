# -*- coding: utf-8 -*-
"""
M0 PoC 第六轮: 破解 403 之谜 —— WAF cookie 流程 + getInterfaceCode 机制
已知事实:
  - 浏览器里搜索成功；服务器复现 403 rtnCode=1002 "InterfaceCode Verification failed."
  - 浏览器 cookie: HWWAFSESID / HWWAFSESTIME（华为 WAF 会话）
  - 详情页网络请求里出现过 getInterfaceCode 调用
本轮验证两条假设:
  A. 先 GET 官网首页（拿 WAF cookie），再带着 cookie 调搜索 API → 能否通过
  B. 从 JS 挖 getInterfaceCode 的调用方式（URL / 参数 / 如何随业务接口传递）
输出: poc/out/waf_flow.json
"""
import json
import re
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

WEB = "https://appgallery.huawei.com"
API = "https://web-drcn.hispace.dbankcloud.com/edge/uowap/index"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
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

findings = {}

# ---- A. WAF cookie 流程: 带持久 cookie jar 先访问官网 ----
with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
    r1 = c.get(WEB + "/")
    cookies_step1 = dict(c.cookies)
    print(f"[A1] 首页: HTTP {r1.status_code}, 获得 cookies: {list(cookies_step1.keys())}")
    # WAF 常见套路: 第一次响应 set HWWAFSESTIME，需要再请求一次换取 HWWAFSESID
    r2 = c.get(WEB + "/")
    cookies_step2 = dict(c.cookies)
    print(f"[A2] 二访: HTTP {r2.status_code}, cookies: {list(cookies_step2.keys())}")

    # 带着官网 cookie 调搜索 API（跨域 cookie 不会自动带，手动注入）
    api_headers = {
        "User-Agent": UA["User-Agent"],
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": WEB + "/",
        "Origin": WEB,
    }
    ra = c.get(API, params=SEARCH_PARAMS, headers=api_headers)
    print(f"[A3] 带 WAF cookie 调搜索 API: HTTP {ra.status_code}")
    print(f"     响应: {ra.text[:200]}")
    findings["waf_flow"] = {
        "cookies_after_homepage": cookies_step2,
        "api_status": ra.status_code,
        "api_body_head": ra.text[:300],
    }
    # 记录 API 域名自己 set 的 cookie（若有）
    findings["waf_flow"]["api_set_cookies"] = dict(ra.cookies)

# ---- B. 从 JS 挖 getInterfaceCode ----
with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
    r = c.get(WEB + "/")
    scripts = re.findall(r'src="([^"]+\.js[^"]*)"', r.text)
    urls = []
    for s in scripts:
        if s.startswith("//"):
            s = "https:" + s
        elif s.startswith("/"):
            s = WEB + s
        elif not s.startswith("http"):
            continue
        urls.append(s)

    contexts = []
    for u in urls:
        try:
            rj = c.get(u)
            if rj.status_code != 200:
                continue
            for m in list(re.finditer(r"getInterfaceCode", rj.text))[:4]:
                start = max(0, m.start() - 500)
                end = min(len(rj.text), m.end() + 500)
                ctx = re.sub(r"\s+", " ", rj.text[start:end])
                contexts.append({"bundle": u.split("/")[-1], "context": ctx})
        except Exception as e:
            print(f"    ! {u} 失败: {e}")

    findings["get_interface_code_contexts"] = contexts
    print(f"\n[B] getInterfaceCode 上下文 {len(contexts)} 段:")
    for it in contexts[:3]:
        print(f"--- [{it['bundle']}] ---")
        print(it["context"][:900])
        print()

(OUT / "waf_flow.json").write_text(
    json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"结果已保存: {OUT / 'waf_flow.json'}")
