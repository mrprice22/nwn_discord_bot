"""Stage 2: token-match over justifications shortlists; the LLM judges the shortlist."""
import json, sys, random, re, concurrent.futures as cf
import yaml
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
from nwnbot import dupes
from evaluate import PROMPT_BOILERPLATE, CACHE
import gen_just as g

ideas = yaml.safe_load(open("/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"))["ideas"]
just = json.load(open(CACHE)); by_id = {i["id"]: i for i in ideas if i.get("id")}
STOP = dupes.STOPWORDS | PROMPT_BOILERPLATE
def J(iid):
    j = (just.get(iid) or {}).get("just", "");  return "" if j.startswith("__ERROR__") else j
P = [dupes.Prepared(i["id"], J(i["id"]), i.get("group") or "", "")
     for i in ideas if i.get("id") and not i.get("dupe_of")]

JUDGE_SYS = (
  "You decide whether a new player report duplicates an existing roadmap item.\n"
  "Two items are DUPLICATES only if fixing one would satisfy the other -- the same "
  "underlying issue or request, however differently worded. Items in the same area "
  "of the game, or that merely sound similar, are NOT duplicates.\n"
  "Reply with exactly one line:\n"
  "  DUPLICATE: <candidate-id> | <one sentence why>\n"
  "  or  NONE | <one sentence why>\n"
  "Be strict. When unsure, answer NONE.")

def judge(src_id, cand_ids):
    def desc(iid):
        i = by_id[iid]
        return f"{i.get('title','')} -- {J(iid)}"
    q = (f"NEW REPORT:\n{desc(src_id)}\n\nEXISTING CANDIDATES:\n"
         + "\n".join(f"- {c}: {desc(c)}" for c in cand_ids))
    return g.call(q if False else q, retries=3), q

def call_judge(src_id, cand_ids):
    def desc(iid):
        i = by_id[iid]; return f"{i.get('title','')} -- {J(iid)}"
    user = (f"NEW REPORT:\n{desc(src_id)}\n\nEXISTING CANDIDATES:\n"
            + "\n".join(f"- {c}: {desc(c)}" for c in cand_ids))
    import urllib.request
    data = json.dumps({"model": g.MODEL,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": JUDGE_SYS},
                     {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": 120}).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request(g.URL, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return (json.load(r)["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            err = str(e)
    return f"__ERROR__ {err}"

def shortlist(src_id, k=5):
    src = dupes.Prepared(src_id, J(src_id), (by_id[src_id].get("group") or ""), "")
    return [c.idea_id for c in dupes.rank(src.title, src.body, P, stopwords=STOP,
                                          title_weight=0.3, limit=k,
                                          exclude=[src_id])]

def verdict(text):
    m = re.match(r"\s*DUPLICATE:\s*([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else None

pos = [(i["id"], i["dupe_of"]) for i in ideas if i.get("dupe_of") and i["dupe_of"] in by_id]
random.seed(11)
neg = random.sample([p.idea_id for p in P], 40)

print("=== 5 known duplicates ===")
jobs = {}
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    for src, tgt in pos:
        sl = shortlist(src)
        jobs[ex.submit(call_judge, src, sl)] = (src, tgt, sl)
    res_pos = {}
    for f in cf.as_completed(jobs):
        src, tgt, sl = jobs[f]; res_pos[src] = (tgt, sl, f.result())
tp = 0
for src, tgt in pos:
    tgt2, sl, out = res_pos[src]
    v = verdict(out)
    ok = (v == tgt)
    tp += ok
    inlist = "in-shortlist" if tgt in sl else "NOT-shortlisted"
    print(f"  {'CORRECT' if ok else 'wrong  '} [{inlist}] {src} -> {tgt}")
    print(f"      {out.splitlines()[0][:150]}")

print(f"\n=== 40 non-duplicates (any DUPLICATE answer is a false positive) ===")
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    jobs2 = {ex.submit(call_judge, s, shortlist(s)): s for s in neg}
    fp = []
    for f in cf.as_completed(jobs2):
        s = jobs2[f]; out = f.result(); v = verdict(out)
        if v:
            fp.append((s, v, out.splitlines()[0][:130]))
print(f"  {len(fp)}/40 false positives ({len(fp)/40*100:.0f}%)")
for s, v, o in fp:
    print(f"    {s} -> {v}\n        {o}")
print(f"\nRECALL {tp}/5   FALSE-POSITIVE RATE {len(fp)/40*100:.0f}%")
