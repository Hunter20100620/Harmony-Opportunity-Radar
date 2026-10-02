# -*- coding: utf-8 -*-
"""华为应用市场（AppGallery）匿名客户端。

逆向流程（JS 拦截器还原，详见 HANDOFF §4.1）:
  1. POST /webedge/getInterfaceCode body {} → JWT
  2. 业务请求头注入 interface-code: {JWT}_{timestamp_ms}
  3. 若返回 403 rtnCode=1002 → 刷新 code 重试

三个接口:
  A. search(keyword)      GET /uowap/index?uri=searchApp|{keyword}
  B. detail(appid)        GET /uowap/index?uri=app|{appid}
  C. comments(appid)      POST /index/commentlist3 body {appid, pageNum, pageSize}
"""
import json
import time

import httpx

API_BASE = "https://web-drcn.hispace.dbankcloud.com/edge"

REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://appgallery.huawei.com/",
    "Origin": "https://appgallery.huawei.com",
}

_SEARCH_PARAMS_BASE = {
    "method": "internal.getTabDetail",
    "serviceType": 20,
    "reqPageNum": 1,
    "maxResults": 25,
    "version": "10.0.0",
    "zone": "",
    "locale": "zh",
}


class AppGalleryError(Exception):
    """华为接口调用异常"""


class AppGalleryClient:
    def __init__(self, request_interval: float = 0.8):
        self._http = httpx.Client(headers=REQ_HEADERS, timeout=20, follow_redirects=True)
        self._code: str | None = None
        self._interval = request_interval
        self._last_request = 0.0

    # ── 频控 ──────────────────────────────────────────────
    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_request = time.time()

    # ── 签名流程 ──────────────────────────────────────────
    def _refresh_code(self):
        """第 1 步：获取 interface code（JWT）"""
        resp = self._http.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
        if resp.status_code != 200:
            raise AppGalleryError(
                f"获取 interface code 失败: HTTP {resp.status_code}"
            )
        raw = resp.text.strip().strip('"')
        try:
            j = resp.json()
            code = j.get("data") if isinstance(j, dict) else j
        except Exception:
            code = raw
        self._code = str(code).strip()
        if not self._code:
            raise AppGalleryError("interface code 为空")

    def _sign_header(self) -> dict:
        """第 2 步：注入 interface-code 头"""
        ts = int(time.time() * 1000)
        return {"interface-code": f"{self._code}_{ts}"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """带自动签名和 1002 重试的一次请求"""
        if not self._code:
            self._refresh_code()

        self._throttle()
        headers = kwargs.pop("headers", {})
        headers.update(self._sign_header())

        resp = self._http.request(method, url, headers=headers, **kwargs)

        # 第 3 步：检测 403/1002 → 刷新 code 重试一次
        if resp.status_code == 403:
            try:
                body = resp.json()
                if body.get("rtnCode") == 1002 or body.get("ret") == 1002:
                    self._refresh_code()
                    self._throttle()
                    headers.update(self._sign_header())
                    resp = self._http.request(method, url, headers=headers, **kwargs)
            except (ValueError, KeyError):
                pass

        return resp

    # ── 接口 A：搜索 ─────────────────────────────────────
    def search(self, keyword: str, max_results: int = 25,
               hard_filter: bool = True) -> list[dict]:
        """按关键词搜索应用，返回去重后的应用列表。

        hard_filter=True 开启硬规则过滤，剔除明显游戏/证券等非工具类应用。
        """
        params = dict(_SEARCH_PARAMS_BASE)
        params["uri"] = f"searchApp|{keyword}"
        params["maxResults"] = max_results

        resp = self._request("GET", f"{API_BASE}/uowap/index", params=params)
        if resp.status_code != 200:
            raise AppGalleryError(f"搜索失败 HTTP {resp.status_code}")

        data = resp.json()
        if data.get("rtnCode") != 0:
            return []

        layout = data.get("layoutData", [])
        apps = []
        seen_pkg: set[str] = set()
        for section in layout:
            for item in section.get("dataList", []):
                pkg = item.get("package", "") or item.get("pkgName", "")
                if not pkg or pkg in seen_pkg:
                    continue
                seen_pkg.add(pkg)
                flat = self._flatten_app(item)
                if hard_filter and not self._is_tool_app(flat):
                    continue
                apps.append(flat)
        return apps

    @staticmethod
    def _is_tool_app(app: dict) -> bool:
        """快速判断候选应用是否为工具/效率类（非游戏、非金融、非娱乐）。"""
        name = (app.get("name", "") or "").lower()
        pkg = (app.get("package", "") or "").lower()
        cat = (app.get("category", "") or "")
        # 包名特征：游戏常见前缀/后缀
        if any(kw in pkg for kw in (".game", ".htgame", ".djtg", ".lenchi", "game.")):
            return False
        if any(kw in name for kw in ("游戏", "手游", "特工", "使命", "王牌战士", "太空")):
            return False
        # 分类特征
        if any(kw in cat for kw in ("休闲", "益智", "射击", "格斗", "棋牌",
                                    "角色", "策略", "竞速", "音乐", "模拟",
                                    "体育", "飞行", "网络游戏", "证券",
                                    "金融", "理财", "保险")):
            return False
        # 明确是工具类的分类保留
        if cat and any(kw in cat for kw in ("工具", "办公", "效率")):
            return True
        return True  # 未命中规则的保留给 LLM 做深度判断

    # ── 接口 B：详情 ─────────────────────────────────────
    def detail(self, appid: str) -> dict | None:
        """获取应用完整详情。"""
        params = dict(_SEARCH_PARAMS_BASE)
        params["uri"] = f"app|{appid}"

        resp = self._request("GET", f"{API_BASE}/uowap/index", params=params)
        if resp.status_code != 200:
            return None

        data = resp.json()
        # 搜索 layoutData 中第一个包含 appid 的条目
        for section in data.get("layoutData", []):
            for item in section.get("dataList", []):
                if item.get("appid") == appid or item.get("package") == appid:
                    return self._flatten_detail(item)
        return None

    # ── 接口 C：评论 ─────────────────────────────────────
    def comments(self, appid: str, page_num: int = 1, page_size: int = 25) -> dict:
        """获取应用评论，返回 {count, total_pages, list[], rating_dist[]}。"""
        body = {"appid": appid, "pageNum": page_num, "pageSize": page_size}
        resp = self._request("POST", f"{API_BASE}/index/commentlist3", json=body)

        if resp.status_code != 200:
            return {"count": 0, "total_pages": 0, "list": [], "rating_dist": []}

        data = resp.json()
        if data.get("rtnCode") != 0:
            return {"count": 0, "total_pages": 0, "list": [], "rating_dist": []}

        # 评论列表字段：list[].{commentInfo, rating, operTime, versionName}
        raw_list = data.get("list", data.get("dataList", []))
        rating_dist = data.get("ratingDstList", data.get("ratingDistList", []))
        return {
            "count": data.get("count", len(raw_list)),
            "total_pages": data.get("totalPages", 1),
            "list": [self._flatten_comment(c) for c in raw_list],
            "rating_dist": rating_dist,
        }

    # ── 字段扁平化 ────────────────────────────────────────
    @staticmethod
    def _flatten_app(item: dict) -> dict:
        """搜索结果的扁平化（只保留 M2 需要的字段）"""
        return {
            "name": item.get("name", ""),
            "appid": item.get("appid", ""),
            "package": item.get("package", item.get("pkgName", "")),
            "score": item.get("score", item.get("rateScore", 0)),
            "downloads": item.get("downCountDesc", ""),
            "version": item.get("appVersionName", ""),
            "category": item.get("kindName", ""),
            "intro": item.get("memo", ""),
            "icon": item.get("icon", ""),
            "size": item.get("size", ""),
        }

    @staticmethod
    def _flatten_detail(item: dict) -> dict:
        """详情响应扁平化"""
        return {
            "name": item.get("name", ""),
            "appid": item.get("appid", ""),
            "package": item.get("package", item.get("pkgName", "")),
            "score": item.get("score", item.get("rateScore", 0)),
            "downloads": item.get("downCountDesc", ""),
            "version": item.get("appVersionName", ""),
            "update_time": item.get("updateTime", item.get("releaseTime", "")),
            "intro": item.get("memo", ""),
            "category": item.get("kindName", ""),
            "comment_count": item.get("COMMENTCOUNT", item.get("commentCount", item.get("COMNUM", 0))),
            "icon": item.get("icon", ""),
            "size": item.get("size", ""),
        }

    @staticmethod
    def _flatten_comment(item: dict) -> dict:
        """评论条目扁平化"""
        return {
            "content": item.get("commentInfo", ""),
            "rating": item.get("rating", 0),
            "time": item.get("operTime", ""),
            "version": item.get("versionName", ""),
        }

    def close(self):
        self._http.close()