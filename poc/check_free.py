import json

with open("data/raw_feed.json", encoding="utf-8") as f:
    feed = json.load(f)

# 找出免费榜来源的应用
free_apps = [a for a in feed["apps"] if "top-free" in a.get("source", "")]
paid_apps = [a for a in feed["apps"] if "top-free" not in a.get("source", "")]

print(f"免费榜应用: {len(free_apps)}")
print(f"付费榜应用: {len(paid_apps)}")
print()

# 按 source 分组
from collections import Counter
srcs = Counter()
for a in feed["apps"]:
    for s in a.get("source", "").split(", "):
        srcs[s.strip()] += 1
print("来源分布:")
for k, v in srcs.most_common():
    print(f"  {k}: {v}")
print()

# 查看 cn/top-free 的前 30 名
cn_free = sorted(
    [a for a in feed["apps"] if "cn/top-free" in a.get("source", "")],
    key=lambda x: x["rank"]
)
print(f"\n=== cn/top-free 前 30 ===")
for a in cn_free[:30]:
    print(f"  #{a['rank']} {a['name']} by {a['artist']}")

# us/top-free 前 30
us_free = sorted(
    [a for a in feed["apps"] if "us/top-free" in a.get("source", "")],
    key=lambda x: x["rank"]
)
print(f"\n=== us/top-free 前 30 ===")
for a in us_free[:30]:
    print(f"  #{a['rank']} {a['name']} by {a['artist']}")