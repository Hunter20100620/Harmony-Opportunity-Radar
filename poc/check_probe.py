import json

with open("data/probe.json", encoding="utf-8") as f:
    d = json.load(f)

for b in d["benchmarks"][:3]:
    print(f"\n=== {b['name']} ===")
    print(f"  keywords: {b['keywords_used']}")
    print(f"  blank_signal: {b['blank_signal']}")
    for c in b["competitors"]:
        print(f"  - {c['name']}: score={c['score']} dls='{c['downloads']}' intro='{c['intro'][:50]}'")
        if c["bad_reviews"]:
            print(f"    bad_reviews: {len(c['bad_reviews'])}")
            print(f"    first: rating={c['bad_reviews'][0]['rating']} '{c['bad_reviews'][0]['content'][:60]}'")

# blank_signal 检查
blank = [b for b in d["benchmarks"] if b["blank_signal"]]
not_blank = [b for b in d["benchmarks"] if not b["blank_signal"]]
print(f"\n=== 总计: {len(d['benchmarks'])} 空白={len(blank)} 非空白={len(not_blank)} ===")

# 差评统计
for b in d["benchmarks"]:
    for c in b["competitors"]:
        if c["bad_reviews"]:
            print(f"\n{b['name']} -> {c['name']}: {len(c['bad_reviews'])} 条差评")
            for r in c["bad_reviews"][:2]:
                print(f"  [{r['rating']}] {r['content'][:80]}")