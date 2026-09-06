"""Evaluate duplicate detection over the LLM justifications, with PURE TOKEN
MATCHING. Same metric as the original calibration so the two are comparable:

  * rank of the true canonical for each of the 5 known dupe_of pairs
  * the false-positive curve: top-1 score when every idea is treated as a
    fresh thread (where its best match is a false positive by construction)
"""
import json, sys, statistics
import yaml
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
from nwnbot import dupes

CACHE = "/tmp/claude-1000/-var-home-james-GIT-nwn-discord-bot/1de36bd5-7784-4e4b-8825-0ef429419ada/scratchpad/justifications.json"

#: Boilerplate the prompt induces in nearly every restatement. It is shared
#: vocabulary with no discriminating power — the justification equivalent of
#: the "Prestige quest:" title template that broke the raw scorer.
PROMPT_BOILERPLATE = frozenset("""
    allow allows enable enables enabling implement implementing provide provides
    player players character characters system systems option options ability
    feature server game mechanic mechanics support supporting additional
    various specific certain based within during through across
""".split())

def load():
    ideas = yaml.safe_load(open("/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"))["ideas"]
    just = json.load(open(CACHE))
    by_id = {i["id"]: i for i in ideas if i.get("id")}
    return ideas, by_id, just

def prep(ideas, just, mode, drop_boiler):
    """mode: 'raw' (title+notes), 'just' (justification only), 'both'."""
    out = []
    for i in ideas:
        iid = i.get("id")
        if not iid or i.get("dupe_of"):
            continue
        j = (just.get(iid) or {}).get("just", "")
        if j.startswith("__ERROR__"):
            j = ""
        if mode == "raw":
            title, body = i.get("title") or "", dupes._flatten_notes(i.get("notes"), 800)
        elif mode == "just":
            title, body = j, ""
        else:
            title = i.get("title") or ""
            body = j + " " + dupes._flatten_notes(i.get("notes"), 800)
        out.append(dupes.Prepared(iid, title, i.get("group") or "", body))
    return out

def prep_one(idea, just, mode):
    iid = idea["id"]
    j = (just.get(iid) or {}).get("just", "")
    if j.startswith("__ERROR__"):
        j = ""
    if mode == "raw":
        return dupes.Prepared(iid, idea.get("title") or "", idea.get("group") or "",
                              dupes._flatten_notes(idea.get("notes"), 800))
    if mode == "just":
        return dupes.Prepared(iid, j, idea.get("group") or "", "")
    return dupes.Prepared(iid, idea.get("title") or "", idea.get("group") or "",
                          j + " " + dupes._flatten_notes(idea.get("notes"), 800))

def run(mode, tw, drop_boiler=True):
    ideas, by_id, just = load()
    stop = dupes.STOPWORDS | (PROMPT_BOILERPLATE if drop_boiler else frozenset())
    P = prep(ideas, just, mode, drop_boiler)
    cases = [(prep_one(i, just, mode), i["dupe_of"]) for i in ideas
             if i.get("dupe_of") and i["dupe_of"] in by_id]

    rows, ranks = [], []
    for src, target in cases:
        ranked = dupes.rank(src.title, src.body, P, stopwords=stop,
                            title_weight=tw, limit=0, exclude=[src.idea_id])
        ids = [c.idea_id for c in ranked]
        pos = ids.index(target) + 1
        ranks.append(pos)
        rows.append((pos, dict((c.idea_id, c.value) for c in ranked)[target],
                     src.idea_id, target, ranked[0].idea_id, ranked[0].value))

    tops = sorted(dupes.rank(p.title, p.body, P, stopwords=stop, title_weight=tw,
                             limit=1, exclude=[p.idea_id])[0].value for p in P)
    n = len(tops)
    fp = {t: sum(1 for v in tops if v >= t) / n for t in (0.2, 0.3, 0.4, 0.5, 0.6)}
    return rows, ranks, fp, n

if __name__ == "__main__":
    for mode in ("raw", "just", "both"):
        for tw in (0.3, 0.6):
            rows, ranks, fp, n = run(mode, tw)
            top1 = sum(1 for r in ranks if r == 1)
            top5 = sum(1 for r in ranks if r <= 5)
            print(f"\n=== mode={mode:5s} title_weight={tw} ===")
            print(f"  ranks of the true canonical: {ranks}   "
                  f"top-1: {top1}/5   top-5: {top5}/5   median rank {statistics.median(ranks)}")
            print("  false positives: " + "  ".join(f"@{t}:{v*100:.0f}%"
                                                    for t, v in fp.items()))
            for pos, sc, a, b, best, bv in rows:
                mark = "HIT " if pos == 1 else f"#{pos:<3}"
                print(f"    {mark} score={sc:.3f}  {a} -> {b}"
                      + ("" if pos == 1 else f"   (best was {best} {bv:.3f})"))
