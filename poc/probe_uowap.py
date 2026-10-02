# -*- coding: utf-8 -*-
"""
M0 PoC 第三轮: 挖掘 /uowap/index 网关全部方法 + 实测匿名调用
背景: JS 上下文里发现老牌内部网关 GET /uowap/index?method=internal.xxx，
      以及 POST /index/getnewhotsearchlist（body: {serviceType}）。
      uowap 网关历史上承载搜索/榜单/详情等「匿名可调」接口 —— 若搜索方法名可挖出，
      逆向路线即告打通。
输出: poc/out/uowap_methods.json + 控制台探测结果
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

# ---- 步骤 1: 重新下载全部 JS bundle，全量提取 internal.* 方法名 ----
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

methods = set()
for u in urls:
    try:
        rj = client.get(u)
        if rj.status_code != 200:
            continue
        # 形态: method: "internal.getTabDetail" / method:"internal.xxx" / 'internal.xxx'
        for m in re.findall(r'["\']internal\.[A-Za-z0-9_]+["\']', rj.text):
            methods.add(m.strip("'\", "))
    except Exception as e:
        print(f"    ! {u} 失败: {e}")

print(f"[1] 挖掘到 {len(methods)} 个 internal.* 方法:")
for m in sorted(methods):
    print("  ", m)

# ---- 步骤 2: 实测 POST /index/getnewhotsearchlist（匿名、无 cookie）----
probes = []
try:
    r1 = client.post(BASE + "/index/getnewhotsearchlist", json={"serviceType": 20})
    body_head = r1.text[:300]
    probes.append({"endpoint": "POST /index/getnewhotsearchlist", "status": r1.status_code, "body_head": body_head})
    print(f"\n[2] POST getnewhotsearchlist: HTTP {r1.status_code}")
    print(f"    {body_head}")
except Exception as e:
    probes.append({"endpoint": "POST /index/getnewhotsearchlist", "error": str(e)})
    print(f"[2] 失败: {e}")

# ---- 步骤 3: 实测老网关 GET /uowap/index?method=internal.getNewHotSearchList ----
try:
    r2 = client.get(
        BASE + "/uowap/index",
        params={"method": "internal.getNewHotSearchList", "serviceType": 20},
    )
    body_head = r2.text[:300]
    probes.append({"endpoint": "GET /uowap/index?method=internal.getNewHotSearchList", "status": r2.status_code, "body_head": body_head})
    print(f"\n[3] GET uowap getNewHotSearchList: HTTP {r2.status_code}")
    print(f"    {body_head}")
except Exception as e:
    probes.append({"endpoint": "GET /uowap/index", "error": str(e)})
    print(f"[3] 失败: {e}")

# ---- 步骤 4: 用 webedge/appinfo 的 POST 形态试探一个已知应用详情 ----
# 华为阅读 pkgName: com.huawei.hwiread（内置应用，存在概率高）
try:
    r3 = client.post(BASE + "/webedge/appinfo", json={"pkgName": "com.huawei.hwiread", "appId": ""})
    body_head = r3.text[:300]
    probes.append({"endpoint": "POST /webedge/appinfo", "status": r3.status_code, "body_head": body_head})
    print(f"\n[4] POST webedge/appinfo: HTTP {r3.status_code}")
    print(f"    {body_head}")
except Exception as e:
    probes.append({"endpoint": "POST /webedge/appinfo", "error": str(e)})
    print(f"[4] 失败: {e}")

findings = {"uowap_methods": sorted(methods), "probes": probes}
(OUT / "uowap_methods.json").write_text(
    json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\n结果已保存: {OUT / 'uowap_methods.json'}")
