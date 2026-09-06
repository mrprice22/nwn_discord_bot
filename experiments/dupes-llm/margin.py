"""Is the top-1 match distinguishable from noise? Test absolute score vs margin."""
import json, sys, statistics
import yaml
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
from nwnbot import dupes
from evaluate import PROMPT_BOILERPLATE, CACHE

ideas = yaml.safe_load(open("/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"))["ideas"]
just = json.load(open(CACHE))
by_id = {i["id"]: i for i in ideas if i.get("id")}
STOP = dupes.STOPWORDS | PROMPT_BOILERPLATE

def J(iid):
    j = (just.get(iid) or {}).get("just", "")
    return "" if j.startswith("__ERROR__") else j

P = [dupes.Prepared(i["id"], J(i["id"]), i.get("group") or "", "")
     for i in ideas if i.get("id") and not i.get("dupe_of")]
pos_cases = [(dupes.Prepared(i["id"], J(i["id"]), i.get("group") or "", ""), i["dupe_of"])
             for i in ideas if i.get("dupe_of") and i["dupe_of"] in by_id]

def top2(src):
    r = dupes.rank(src.title, src.body, P, stopwords=STOP, title_weight=0.3,
                   limit=3, exclude=[src.idea_id])
    return r

print("--- the 5 known duplicates ---")
pos_rows = []
for src, target in pos_cases:
    r = top2(src)
    ids = [c.idea_id for c in r]
    hit = r[0].idea_id == target
    margin = r[0].value - r[1].value
    ratio = r[0].value / r[1].value if r[1].value else 99
    pos_rows.append((hit, r[0].value, margin, ratio))
    print(f"  {'HIT ' if hit else 'miss'} top1={r[0].value:.3f} margin={margin:+.3f} "
          f"ratio={ratio:.2f}  {src.idea_id} -> {target} (got {r[0].idea_id})")

print("\n--- all 404 as fresh threads (top-1 is a false positive) ---")
neg = []
for p in P:
    r = top2(p)
    neg.append((r[0].value, r[0].value - r[1].value,
                r[0].value / r[1].value if r[1].value else 99))
neg.sort(key=lambda x: -x[1])
margins = sorted(x[1] for x in neg)
ratios = sorted(x[2] for x in neg)
n = len(neg)
print("  margin  p50 %.3f p90 %.3f p95 %.3f p99 %.3f" %
      tuple(margins[int(p*n)] for p in (.5,.9,.95,.99)))
print("  ratio   p50 %.2f p90 %.2f p95 %.2f p99 %.2f" %
      tuple(ratios[int(p*n)] for p in (.5,.9,.95,.99)))

print("\n--- operating points (hits found / false positives fired) ---")
hits = [r for r in pos_rows if r[0]]
for name, idx, cuts in (("abs score", 1, (0.20,0.25,0.30,0.40,0.50)),
                        ("margin", 2, (0.02,0.05,0.08,0.12,0.20)),
                        ("ratio", 3, (1.1,1.3,1.5,2.0,3.0))):
    print(f"  by {name}:")
    for c in cuts:
        got = sum(1 for r in hits if r[idx] >= c)
        fp = sum(1 for x in neg if x[idx-1] >= c)
        print(f"    >= {c:<5}: {got}/5 real duplicates, {fp:>3}/404 false ({fp/n*100:.0f}%)")
