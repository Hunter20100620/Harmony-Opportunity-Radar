# -*- coding: utf-8 -*-
"""
M0 PoC 第八轮: 详情 API 验证 + 评论接口挖掘
已知: 详情接口 = 同一网关 GET /uowap/index?method=internal.getTabDetail&uri=app|{appid}
本轮:
  A. 用已打通的 interface-code 流程调详情接口，检查响应里的字段
     （更新时间 / 版本 / 开发者 / 评分分布 / 是否内嵌评论）
  B. 解析 webpack chunk 映射，下载全部 JS 分块，找评论相关接口
     （评论区代码在懒加载分块里，首页 6 个 bundle 里没有）
输出: poc/out/detail_verified.json + 控制台评论接口线索
"""
import json
import re
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

API_BASE = "https://web-drcn.hispace.dbankcloud.com/edge"
WEB = "https://appgallery.huawei.com"
APPID = "C103571573"  # 独孤计时器倒计时（上轮搜索结果）

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": WEB + "/",
    "Origin": WEB,
}

findings = {}
with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
    # ---- A. 详情接口 ----
    r1 = c.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
    code = r1.text.strip().strip('"')
    ic = f"{code}_{int(time.time() * 1000)}"

    rd = c.get(
        f"{API_BASE}/uowap/index",
        params={
            "method": "internal.getTabDetail",
            "serviceType": 20,
            "reqPageNum": 1,
            "uri": f"app|{APPID}",
            "maxResults": 25,
            "version": "10.0.0",
            "zone": "",
            "locale": "zh",
        },
        headers={"interface-code": ic},
    )
    print(f"[A] 详情 API: HTTP {rd.status_code}, {len(rd.text)} bytes")
    if rd.status_code == 200:
        dd = rd.json()
        findings["detail_response"] = dd

        # 递归找应用详情字典（含评论/更新时间特征字段）
        def walk(obj, path="root", depth=0):
            hits = []
            if depth > 8:
                return hits
            if isinstance(obj, dict):
                ks = set(obj.keys())
                if ks & {"updateTime", "releaseTime", "commentCount", "developerName", "commentList", "rating"}:
                    hits.append((path, sorted(ks)[:30]))
                for k, v in obj.items():
                    hits.extend(walk(v, f"{path}.{k}", depth + 1))
            elif isinstance(obj, list) and obj:
                hits.extend(walk(obj[0], f"{path}[0]", depth + 1))
            return hits

        hits = walk(dd)
        print(f"    找到 {len(hits)} 个疑似详情/评论节点:")
        for p, ks in hits[:8]:
            print(f"    {p}: {ks}")
        # 检查顶层关键字段（应用条目本体）
        def find_app(obj, depth=0):
            if depth > 8:
                return None
            if isinstance(obj, dict):
                if "appid" in obj or "package" in obj:
                    return obj
                for v in obj.values():
                    r = find_app(v, depth + 1)
                    if r:
                        return r
            elif isinstance(obj, list) and obj:
                for v in obj[:3]:
                    r = find_app(v, depth + 1)
                    if r:
                        return r
            return None

        app = find_app(dd)
        if app:
            print("\n    应用条目关键字段:")
            for k in ("name", "package", "appid", "score", "appVersionName", "kindName",
                      "downCountDesc", "memo", "COMNUM", "commentCount", "updateTime", "releaseTime"):
                if k in app:
                    print(f"      {k}: {str(app[k])[:80]}")

    # ---- B. webpack chunk 挖掘评论接口 ----
    print("\n[B] webpack chunk 分析...")
    r = c.get(WEB + "/")
    scripts = re.findall(r'src="([^"]+\.js[^"]*)"', r.text)
    main_js = [s for s in scripts if "manifest" in s or "app." in s]
    # webpack 的 chunk 名映射一般在主文件里，形如 {0:"xxx",1:"yyy"} 或 e.p+e.u(...)
    chunk_map = {}
    for s in scripts:
        if s.startswith("//"):
            s = "https:" + s
        elif s.startswith("/"):
            s = WEB + s
        elif not s.startswith("http"):
            continue
        try:
            rj = c.get(s)
            if rj.status_code != 200:
                continue
            # 形态: {"7":"eaee8b103895b2bba41d"} 或 7:"eaee8b..."
            for m in re.finditer(r'[,{](\d{1,4}):"([0-9a-f]{16,22})"', rj.text):
                chunk_map[m.group(1)] = m.group(2)
        except Exception:
            continue
    print(f"    解析出 {len(chunk_map)} 个 chunk 哈希")

    # 从首页 HTML 拿 JS 公共路径（bundle URL 形如 .../js/7.eaee8b...js）
    base_prefix = re.search(r'(https://[a-z0-9.\-/]+/js/)[\w.]+\.js', " ".join(s for s in scripts if "dbankcdn" in s))
    prefix = base_prefix.group(1) if base_prefix else None
    print(f"    chunk URL 前缀: {prefix}")

    comment_apis = set()
    if prefix:
        # 只下载与评论最可能相关的分块太多，全下（~几十个）成本可控，上限 40
        for idx, (cid, h) in enumerate(list(chunk_map.items())[:40]):
            try:
                rc = c.get(f"{prefix}{cid}.{h}.js")
                if rc.status_code != 200:
                    continue
                # 评论相关接口特征词
                for kw in ("comment", "Comment", "review", "Review", "appraise", "Appraise"):
                    if kw in rc.text:
                        for m in re.findall(r'"(/[a-zA-Z0-9_\-/]*(?:comment|review|appraise)[a-zA-Z0-9_\-/]*)"', rc.text, re.I):
                            if 3 < len(m) < 60:
                                comment_apis.add(m)
                        # uowap method 形态
                        for m in re.findall(r'["\']internal\.([A-Za-z0-9_]*[Cc]omment[A-Za-z0-9_]*)["\']', rc.text):
                            comment_apis.add("internal." + m)
                        for m in re.findall(r'["\']internal\.([A-Za-z0-9_]*[Rr]eview[A-Za-z0-9_]*)["\']', rc.text):
                            comment_apis.add("internal." + m)
            except Exception:
                continue

    findings["chunk_count"] = len(chunk_map)
    findings["comment_apis"] = sorted(comment_apis)
    print(f"    评论相关接口线索: {sorted(comment_apis) if comment_apis else '（未找到，需进一步手段）'}")

(OUT / "detail_verified.json").write_text(
    json.dumps({k: v for k, v in findings.items() if k != "detail_response"},
               ensure_ascii=False, indent=2),
    encoding="utf-8",
)
# 详情响应单独存，体积大
(OUT / "detail_raw.json").write_text(
    json.dumps(findings.get("detail_response", {}), ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(f"\n结果已保存: {OUT / 'detail_verified.json'} 与 detail_raw.json")
