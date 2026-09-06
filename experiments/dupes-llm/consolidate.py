"""Turn the raw sweep into a deduplicated, ranked list of pairs to adjudicate."""
import json, os, re, sys
import yaml
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
ideas = yaml.safe_load(open("/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"))["ideas"]
by_id = {i["id"]: i for i in ideas if i.get("id")}
just = json.load(open(f"{HERE}/justifications.json"))
raw = json.load(open(f"{HERE}/sweep-raw.json"))

def J(i): 
    j = (just.get(i) or {}).get("just", "")
    return "" if j.startswith("__ERROR__") else j

known = {tuple(sorted((i["id"], i["dupe_of"]))) for i in ideas
         if i.get("dupe_of") and i["dupe_of"] in by_id}

pairs = {}
errors = 0
for src, res in raw.items():
    if not res or res.get("answer", "").startswith("__ERROR__"):
        errors += 1
        continue
    ans = res["answer"]
    m = re.match(r"\s*DUPLICATE:\s*([A-Za-z0-9_.-]+)\s*\|?\s*(.*)", ans)
    if not m:
        continue
    tgt, why = m.group(1), m.group(2).strip()
    if tgt not in by_id or tgt == src:
        continue
    scores = dict(tuple(x) for x in res["shortlist"])
    key = tuple(sorted((src, tgt)))
    entry = pairs.setdefault(key, {"reasons": [], "score": scores.get(tgt, 0.0),
                                   "both_ways": False})
    entry["reasons"].append({"from": src, "why": why})
    entry["score"] = max(entry["score"], scores.get(tgt, 0.0))

for k, v in pairs.items():
    v["both_ways"] = len(v["reasons"]) > 1

def grp(i): return by_id[i].get("group") or ""
out = []
for (a, b), v in pairs.items():
    out.append({
        "id": f"{a}~{b}",
        "a": a, "b": b,
        "a_title": by_id[a].get("title", ""), "b_title": by_id[b].get("title", ""),
        "a_just": J(a), "b_just": J(b),
        "a_group": grp(a), "b_group": grp(b),
        "a_status": by_id[a].get("status", ""), "b_status": by_id[b].get("status", ""),
        "a_type": by_id[a].get("type", ""), "b_type": by_id[b].get("type", ""),
        "same_group": grp(a) == grp(b),
        "score": round(v["score"], 4),
        "both_ways": v["both_ways"],
        "already_merged": (a, b) in known or (b, a) in known,
        "why": v["reasons"][0]["why"],
        "why2": v["reasons"][1]["why"] if len(v["reasons"]) > 1 else "",
    })
# Mutual agreement first, then score.
out.sort(key=lambda r: (not r["both_ways"], -r["score"]))
json.dump(out, open(f"{HERE}/suggestions.json", "w"), indent=1)
print(f"{len(raw)} judged, {errors} errors")
print(f"{len(out)} distinct pairs proposed")
print(f"  {sum(1 for r in out if r['both_ways'])} where BOTH items named the other")
print(f"  {sum(1 for r in out if r['same_group'])} within the same group")
print(f"  {sum(1 for r in out if r['already_merged'])} already merged (ground truth recovered)")
for r in out[:15]:
    tag = "BOTH" if r["both_ways"] else "one "
    print(f"  [{tag}] {r['score']:.2f}  {r['a']}  ~  {r['b']}")
    print(f"          {r['a_title'][:70]}")
    print(f"          {r['b_title'][:70]}")
