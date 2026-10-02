import json

with open("data/probe.json", encoding="utf-8") as f:
    d = json.load(f)

print(f"探测时间: {d['probed_at']}")
total_competitors = 0
total_comments = 0
total_bad = 0

for b in d["benchmarks"]:
    name = b["name"]
    blanks = [c for c in b["competitors"] if not c["name"]]
    with_comments = [c for c in b["competitors"] if c["bad_review_count"] > 0]
    bad_total = sum(c["bad_review_count"] for c in b["competitors"])
    total_competitors += b["competitor_count"]
    total_comments += sum(len(c["bad_reviews"]) + 0 for c in b["competitors"])
    total_bad += bad_total
    
    print(f"\n{b['competitor_count']} 竞品 | {bad_total} 差评 | {name}")
    for c in b["competitors"][:2]:
        dls = c.get("downloads", "")
        score = c.get("score", "")
        bad = c.get("bad_review_count", 0)
        print(f"  [{score}] {c['name']} dls={dls} 差评={bad}")

print(f"\n{'='*50}")
print(f"总标杆: {len(d['benchmarks'])}")
print(f"总竞品: {total_competitors}")
print(f"总差评: {total_bad}")
print(f"空白信号: {sum(1 for b in d['benchmarks'] if b['blank_signal'])}")