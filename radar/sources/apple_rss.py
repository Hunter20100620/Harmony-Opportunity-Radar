# -*- coding: utf-8 -*-
"""Apple 官方 RSS 榜单拉取（M0 已验证可用，零鉴权零风控）。

接口: https://rss.applemarketingtools.com/api/v2/{region}/apps/{chart}/{limit}/apps.json
  region: cn / us
  chart:  top-free / top-paid
  limit:  1~200
返回 feed.results[]: name / artistName / id / url
注意: 必须开 follow_redirects（该地址 30x 跳转一次）。
"""
import time

import httpx

RSS_BASE = "https://rss.applemarketingtools.com/api/v2"

# 免费榜（尤其是 cn/top-free）偶发读超时，给更长超时和重试
_CHART_TIMEOUT = {"cn": {"top-free": 60}, "default": 30}
_CHART_RETRIES = {"cn": {"top-free": 3}, "default": 2}


def fetch_chart(region: str, chart: str, limit: int = 100) -> list[dict]:
    """拉取单个榜单，返回精简后的应用 dict 列表。

    只保留后续 LLM 精选需要的字段。
    内置重试：cn/top-free 极易超时，单独加长超时和重试次数。
    """
    url = f"{RSS_BASE}/{region}/apps/{chart}/{limit}/apps.json"
    timeout = _CHART_TIMEOUT.get(region, {}).get(chart, _CHART_TIMEOUT["default"])
    max_retries = _CHART_RETRIES.get(region, {}).get(chart, _CHART_RETRIES["default"])

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(trust_env=False) as client:
                resp = client.get(url, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            results = resp.json().get("feed", {}).get("results", [])
            apps = []
            for i, item in enumerate(results):
                apps.append({
                    "rank": i + 1,
                    "name": item.get("name", "").strip(),
                    "artist": item.get("artistName", "").strip(),
                    "apple_id": item.get("id", ""),
                    "url": item.get("url", ""),
                    "source": f"apple/{region}/{chart}",
                })
            return apps
        except (httpx.HTTPError, KeyError, ValueError) as e:
            last_err = e
            if attempt < max_retries:
                wait = min(1.5 ** attempt, 5)
                print(f"  [retry] {region}/{chart} 第 {attempt} 次失败({e}), "
                      f"{wait:.0f}秒后重试…")
                time.sleep(wait)
    raise RuntimeError(f"拉取 {region}/{chart} 失败 ({max_retries}次重试): {last_err}")


def fetch_all(regions: list[str], charts: list[str], limit: int = 100) -> list[dict]:
    """拉取多个地区 × 多个榜单的全部应用，按 apple_id 主键去重。

    去重规则: 优先用 apple_id 作为主键（缺失时回退到名称小写），
    同名不同 apple_id 的应用各自保留，避免名称去重造成静默信息丢失；
    合并其出现来源，并保留原始来源字段。
    """
    seen: dict[str, dict] = {}  # key -> app（跨榜去重用）
    for region in regions:
        for chart in charts:
            try:
                batch = fetch_chart(region, chart, limit)
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                # 单个榜单失败不炸整个流水线，打印告警继续
                print(f"[warn] 拉取 {region}/{chart} 失败: {e}")
                continue
            for app in batch:
                aid = str(app.get("apple_id", "") or "").strip()
                key = f"id:{aid}" if aid else f"name:{app['name'].lower()}"
                if key in seen:
                    seen[key]["source"] += f", {app['source']}"  # 合并来源
                    seen[key]["source_charts"] = seen[key].get("source_charts", []) + [app["source"]]
                else:
                    app["source_charts"] = [app["source"]]
                    seen[key] = app
    return list(seen.values())
