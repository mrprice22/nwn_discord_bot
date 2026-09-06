"""One-off: judge every idea against its top-5 justification shortlist.

Produces `suggestions.json`: every pair the judge called a duplicate, plus the
near-misses (judged NONE but scoring high), for the admin to adjudicate. Read
only -- writes nothing to roadmap.yaml and nothing to the bot's state.
"""
import json, os, sys, re, time, urllib.request, concurrent.futures as cf
import yaml
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
sys.path.insert(0, HERE)
from nwnbot import dupes
import gen_just as g
from evaluate import PROMPT_BOILERPLATE

ROADMAP = "/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"
OUT = os.path.join(HERE, "sweep-raw.json")

JUDGE_SYS = (
  "You decide whether a roadmap item duplicates another roadmap item.\n"
  "Two items are DUPLICATES if doing one would substantially satisfy the other -- "
  "the same underlying issue, request or feature, however differently worded. "
  "Items that merely touch the same area of the game are NOT duplicates.\n"
  "Reply with exactly one line:\n"
  "  DUPLICATE: <candidate-id> | <one sentence why>\n"
  "  NONE | <one sentence why>\n"
  "When genuinely unsure, prefer DUPLICATE and say why -- a human reviews every answer.")

ideas = yaml.safe_load(open(ROADMAP))["ideas"]
by_id = {i["id"]: i for i in ideas if i.get("id")}
just = json.load(open(os.path.join(HERE, "justifications.json")))
STOP = dupes.STOPWORDS | PROMPT_BOILERPLATE

def J(iid):
    j = (just.get(iid) or {}).get("just", "")
    return "" if j.startswith("__ERROR__") else j

P = [dupes.Prepared(i["id"], J(i["id"]), i.get("group") or "", "")
     for i in ideas if i.get("id") and not i.get("dupe_of")]

def desc(iid):
    return f"{by_id[iid].get('title','')} -- {J(iid)}"

def call(user, sys_prompt=JUDGE_SYS, retries=3):
    data = json.dumps({"model": g.MODEL,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}],
        "temperature": 0, "max_tokens": 140}).encode()
    err = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(g.URL, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                return (json.load(r)["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            err = str(e); time.sleep(2 * (attempt + 1))
    return f"__ERROR__ {err}"

def one(src_id):
    sl = dupes.rank(J(src_id), "", P, stopwords=STOP, title_weight=0.3,
                    limit=5, exclude=[src_id])
    if not sl:
        return src_id, None
    user = (f"ITEM:\n{desc(src_id)}\n\nCANDIDATES:\n"
            + "\n".join(f"- {c.idea_id}: {desc(c.idea_id)}" for c in sl))
    return src_id, {"shortlist": [(c.idea_id, round(c.value, 4)) for c in sl],
                    "answer": call(user)}

def main():
    cache = json.load(open(OUT)) if os.path.exists(OUT) else {}
    todo = [p.idea_id for p in P
            if p.idea_id not in cache or cache[p.idea_id] is None
            or cache[p.idea_id].get("answer", "").startswith("__ERROR__")]
    print(f"{len(P)} ideas, {len(todo)} to judge", flush=True)
    t0, done = time.time(), 0
    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "6"))) as ex:
        for src, res in ex.map(one, todo):
            cache[src] = res
            done += 1
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                print(f"  {done}/{len(todo)}  {el:.0f}s elapsed, "
                      f"~{el/done*(len(todo)-done):.0f}s left", flush=True)
                json.dump(cache, open(OUT, "w"), indent=1)
    json.dump(cache, open(OUT, "w"), indent=1)
    print("done")

if __name__ == "__main__":
    main()
