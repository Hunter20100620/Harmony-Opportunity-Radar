# -*- coding: utf-8 -*-
"""
M0 PoC 第四轮: 找到 uowap 网关的正确参数组合 + API host
背景: 第三轮已确认 GET /uowap/index?method=internal.getNewHotSearchList&serviceType=20
      返回 {"rtnCode":40402,"rtnDesc":"check param fail."} —— 网关匿名可达，仅参数不对。
本轮:
  A. 从 JS 里挖 getService/getServicePrivate 实例的 baseURL（API host 可能另有其人）
  B. 穷举 serviceType 调 getNewHotSearchList
  C. 试探 completeSearchWord 的关键词参数名
输出: poc/out/uowap_probe2.json
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
    "Referer": BASE + "/",
}

client = httpx.Client(headers=UA, timeout=20, follow_redirects=True)

# ---- A. 挖 baseURL / API host 定义 ----
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

base_urls = set()
for u in urls:
    try:
        rj = client.get(u)
        if rj.status_code != 200:
            continue
        # axios 实例定义: baseURL: "https://..." 或 servicePath/apiHost 等变量赋值
        for m in re.findall(r'baseURL\s*[:=]\s*["\']([^"\']+)["\']', rj.text):
            base_urls.add(m)
        for m in re.findall(r'["\'](https://[a-z0-9.\-]+/(?:api|uowap|mwsearch)[a-zA-Z0-9_\-/]*)["\']', rj.text):
            base_urls.add(m)
    except Exception as e:
        print(f"    ! {u} 失败: {e}")

print(f"[A] 发现 {len(base_urls)} 个 baseURL/host 候选:")
for b in sorted(base_urls):
    print("  ", b)

# ---- B. 穷举 serviceType 调 getNewHotSearchList ----
print("\n[B] getNewHotSearchList 参数穷举:")
results_b = []
for st in (1, 2, 7, 20, 25, 26):
    try:
        rb = client.get(
            BASE + "/uowap/index",
            params={"method": "internal.getNewHotSearchList", "serviceType": st},
        )
        head = rb.text[:160].replace("\n", " ")
        ok = '"rtnCode":0' in rb.text or '"rtnCode": 0' in rb.text
        results_b.append({"serviceType": st, "status": rb.status_code, "ok": ok, "body_head": head})
        print(f"  serviceType={st}: HTTP {rb.status_code} {'✓成功' if ok else ''} {head[:120]}")
        if ok:
            break  # 找到就停
    except Exception as e:
        results_b.append({"serviceType": st, "error": str(e)})
        print(f"  serviceType={st}: 失败 {e}")

# ---- C. completeSearchWord 关键词参数名试探 ----
print("\n[C] completeSearchWord 参数名试探:")
results_c = []
for kw_param in ("searchWord", "keyword", "inputWord", "word", "q"):
    try:
        rc = client.get(
            BASE + "/uowap/index",
            params={
                "method": "internal.completeSearchWord",
                "serviceType": 20,
                kw_param: "倒计时",
            },
        )
        head = rc.text[:200].replace("\n", " ")
        # rtnCode==0 视为成功
        ok = '"rtnCode":0' in rc.text
        results_c.append({"kw_param": kw_param, "status": rc.status_code, "ok": ok, "body_head": head})
        print(f"  {kw_param}=倒计时: HTTP {rc.status_code} {'✓成功' if ok else ''} {head[:140]}")
        if ok:
            print(f"  >>> 完整响应: {rc.text[:800]}")
            break
    except Exception as e:
        results_c.append({"kw_param": kw_param, "error": str(e)})
        print(f"  {kw_param}: 失败 {e}")

findings = {"base_urls": sorted(base_urls), "hotlist": results_b, "complete_search": results_c}
(OUT / "uowap_probe2.json").write_text(
    json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\n结果已保存: {OUT / 'uowap_probe2.json'}")
