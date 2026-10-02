import json

with open("data/probe.json", encoding="utf-8") as f:
    d = json.load(f)

for b in d["benchmarks"]:
    name = b["name"]
    n = b["competitor_count"]
    blank = b["blank_signal"]
    competitors = b["competitors"]
    print(f"\n{'[空白]' if blank else '      '} {name} ({n} 竞品)")
    for c in competitors:
        cat = c.get("category", "")
        bad = c.get("bad_review_count", 0)
        print(f"      [{c['score']}] {c['name']} 类别={cat} 差评={bad}")

print(f"\n=== 总计: {len(d['benchmarks'])} 标杆, "
      f"{sum(b['competitor_count'] for b in d['benchmarks'])} 竞品, "
      f"{sum(1 for b in d['benchmarks'] if b['blank_signal'])} 空白信号 ===")