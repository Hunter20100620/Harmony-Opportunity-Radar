import sys, json
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient

c = AppGalleryClient()
try:
    for kw in ["计时器", "专注工具", "番茄钟", "待办清单", "习惯追踪"]:
        apps = c.search(kw, max_results=5)
        if apps:
            a = apps[0]
            print(f"关键词 '{kw}' → 首条: {a['name']} (评分 {a['score']}, 下载 {a['downloads']})")
            cmt = c.comments(a["appid"])
            if cmt["list"]:
                bad = [x for x in cmt["list"] if x["rating"] <= 3]
                print(f"          评论总数 {cmt['count']}, 差评 {len(bad)} 条, 最新: {bad[0]['content'][:60] if bad else cmt['list'][0]['content'][:60]}")
        else:
            print(f"关键词 '{kw}' → 无结果")
except Exception as e:
    print(f"失败: {e}")
    import traceback; traceback.print_exc()
finally:
    c.close()