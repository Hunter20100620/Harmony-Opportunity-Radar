import json

with open("data/probe.json", encoding="utf-8") as f:
    d = json.load(f)

# 检查噪声关键词是否全部消失
noise_kw = ["顶级特工", "太空秘密", "国信金太阳", "天天爱烹饪",
            "黑暗泰坦", "小太阳运动", "宝宝爱交通", "微聊天记录",
            "往约管理", "债务规划", "智在记录"]
all_noise = [c["name"] for b in d["benchmarks"] for c in b["competitors"]]
found_noise = [k for k in noise_kw if any(k in n for n in all_noise)]
print(f"残余噪声: {found_noise if found_noise else '无'} ✓")

# 统计差评真实来源
bad_apps = [(b["name"], c["name"], c["bad_review_count"], c["bad_reviews"][0]["content"][:60] if c["bad_reviews"] else "")
            for b in d["benchmarks"] for c in b["competitors"] if c["bad_reviews"]]
print(f"\n真实差评来源 ({len(bad_apps)} 个):")
for bname, cname, n, first in bad_apps:
    print(f"  {bname} → {cname}: {n}条, 首条: {first}")

# 空白标杆
blanks = [b["name"] for b in d["benchmarks"] if b["blank_signal"]]
print(f"\n空白信号标杆 ({len(blanks)}):")
for b in blanks:
    print(f"  [空白] {b}")

# 竞品类别分布
from collections import Counter
cats = Counter(c.get("category", "") for b in d["benchmarks"] for c in b["competitors"])
print(f"\n竞品类别分布:")
for k, v in cats.most_common():
    print(f"  {k}: {v}")