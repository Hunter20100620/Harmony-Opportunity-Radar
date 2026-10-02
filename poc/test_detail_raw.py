import sys, json
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient

c = AppGalleryClient()
try:
    # 试一个评分高的 app
    apps = c.search("番茄", max_results=5)
    for a in apps:
        print(f"name={a['name']} appid={a['appid']} score={a['score']} download={a['downloads']}")
    if apps:
        a = apps[0]
        # 调原始 detail（不加 flatten）
        import httpx, time
        from radar.sources.appgallery import API_BASE, _SEARCH_PARAMS_BASE
        # 刷新 code
        r1 = c._http.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
        code = r1.text.strip().strip('"')
        ic = f"{code}_{int(time.time() * 1000)}"
        
        params = dict(_SEARCH_PARAMS_BASE)
        params["uri"] = f"app|{a['appid']}"
        rd = c._http.get(f"{API_BASE}/uowap/index", params=params, headers={"interface-code": ic})
        data = rd.json()
        # 找所有包含 score/comment 字段的节点
        def find(obj, path="", depth=0):
            if depth > 6: return
            if isinstance(obj, dict):
                keys = list(obj.keys())
                if any(k in str(keys).lower() for k in ["score", "comment", "rating", "downcount"]):
                    print(f"\n{path}:")
                    for k, v in list(obj.items())[:15]:
                        print(f"  {k}: {str(v)[:80]}")
                for k, v in obj.items():
                    find(v, f"{path}.{k}", depth+1)
            elif isinstance(obj, list) and obj and len(obj) < 5:
                find(obj[0], f"{path}[0]", depth+1)
        find(data)
        
except Exception as e:
    import traceback; traceback.print_exc()
finally:
    c.close()