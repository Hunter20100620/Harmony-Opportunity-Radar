import sys, json, time
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient, API_BASE, _SEARCH_PARAMS_BASE
import httpx

c = AppGalleryClient()

# 先拿米拍摄影 appid
apps = c.search("米拍摄影", max_results=3)
for a in apps:
    print(f"search: {a['name']} appid={a['appid']} pkg={a['package']}")
    cmt = c.comments(a['appid'])
    print(f"  comments(count={cmt['count']}): {len(cmt['list'])} 条")
    if cmt['list']:
        for r in cmt['list'][:3]:
            print(f"    [{r['rating']}] {r['content'][:60]}")
    else:
        # 试 raw call
        body = {"appid": a["appid"], "pageNum": 1, "pageSize": 25}
        r1 = c._http.post(f"{API_BASE}/webedge/getInterfaceCode", json={})
        code = r1.text.strip().strip('"')
        ic = f"{code}_{int(time.time() * 1000)}"
        rc = c._http.post(f"{API_BASE}/index/commentlist3", json=body, headers={"interface-code": ic})
        print(f"  raw comments: HTTP {rc.status_code}, len={len(rc.text)}")
        try:
            d = rc.json()
            print(f"  rtnCode={d.get('rtnCode')} count={d.get('count')}")
            if d.get('list'):
                for r in d['list'][:2]:
                    print(f"    [{r.get('rating')}] {r.get('commentInfo','')[:60]}")
        except:
            print(f"  raw: {rc.text[:200]}")
c.close()