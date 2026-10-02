import sys
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient

c = AppGalleryClient()
# 测 correct appid format for detail
apps = c.search("番茄", max_results=5)
for a in apps:
    print(f"search: {a['name']} appid={a['appid']}")
    # detail 试嵌套找法
    import httpx, time
    from radar.sources.appgallery import API_BASE, _SEARCH_PARAMS_BASE
    params = dict(_SEARCH_PARAMS_BASE)
    params["uri"] = f"app|{a['appid']}"
    resp = c._http.get(f"{API_BASE}/uowap/index", params=params, headers=c._sign_header())
    data = resp.json()
    # 遍历 layoutData 找可读字段
    for section in data.get("layoutData", []):
        for item in section.get("dataList", []):
            if item.get("appid") == a['appid']:
                print(f"  detail appid match found!")
                for k in ["name","appid","score","rateScore","downCountDesc","appVersionName","updateTime","COMMENTCOUNT","commentCount","COMNUM","memo","kindName"]:
                    if k in item:
                        print(f"    {k}: {item[k]}")
c.close()