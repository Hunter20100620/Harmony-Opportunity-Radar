import sys
sys.path.insert(0, ".")
from radar.sources.appgallery import AppGalleryClient

c = AppGalleryClient()
try:
    apps = c.search("倒计时", max_results=5)
    print(f"搜索 '倒计时': {len(apps)} 个结果")
    if apps:
        a = apps[0]
        print(f"  {a['name']} | {a['appid']} | 评分 {a['score']} | 下载 {a['downloads']}")
        print(f"  intro: {a['intro'][:80]}")
        detail = c.detail(a['appid'])
        if detail:
            print(f"详情: 版本 {detail['version']} | 更新 {detail['update_time']} | 评论数 {detail['comment_count']}")
        # 评论测试
        # 部分 appid 格式不同
        cmt = c.comments(a['appid'])
        print(f"评论: 共 {cmt['count']} 条，本页 {len(cmt['list'])} 条")
        if cmt['list']:
            print(f"  最新: rating={cmt['list'][0]['rating']} '{cmt['list'][0]['content'][:60]}'")
    print("\n✓ 客户端全部接口可用")
except Exception as e:
    print(f"✗ 失败: {e}")
    import traceback
    traceback.print_exc()
finally:
    c.close()