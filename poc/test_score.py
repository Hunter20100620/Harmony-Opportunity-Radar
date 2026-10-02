import sys
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient

c = AppGalleryClient()
# 测一个搜索结果的score字段真实值
apps = c.search("倒计时", max_results=5)
for a in apps:
    print(f"search: {a['name']} score={a['score']} download={a['downloads']} appid={a['appid']}")
    d = c.detail(a['appid'])
    if d:
        print(f"  detail: score={d['score']} comment_count={d['comment_count']}")
    # 试评论
    try:
        cmt = c.comments(a['appid'])
        print(f"  comments: count={cmt['count']}, list={len(cmt['list'])}")
        if cmt['list']:
            print(f"    first: rating={cmt['list'][0]['rating']} '{cmt['list'][0]['content'][:50]}'")
    except Exception as e:
        print(f"  comments err: {e}")
c.close()