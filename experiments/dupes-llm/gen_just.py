"""Experiment: generate a canonical restatement ("justification") per roadmap
idea with the local LLM, then score duplicates with PURE TOKEN MATCHING over
those restatements. Nothing here writes to roadmap.yaml or the repo."""
import json, hashlib, os, sys, time, urllib.request, concurrent.futures as cf
import yaml
sys.path.insert(0, "/var/home/james/GIT/nwn_discord_bot")
from nwnbot import dupes

URL = "http://10.42.0.83:8080/v1/chat/completions"
MODEL = "D:\\models\\Qwen3.6-35B-A3B-Q4_K_M.gguf"
CACHE = os.path.join(os.path.dirname(__file__), "justifications.json")

SYSTEM = (
    "You normalise game-server roadmap items into a canonical form so that two "
    "reports of the SAME underlying issue come out using the SAME words.\n"
    "Rules:\n"
    "- Output ONE line, 12-20 words. No labels, no preamble, no quotes.\n"
    "- Use the most generic standard term for each thing. Prefer common nouns "
    "over proper nouns and server-specific names (say 'teleport destination', "
    "not 'Well-of-Eru'; say 'companion', not 'henchman' or a pet's name).\n"
    "- Name the game subsystem, the actor, and what is wanted or wrong.\n"
    "- Describe the underlying need, not the phrasing of the request."
)

def build(idea):
    body = dupes._flatten_notes(idea.get("notes"), 600)
    txt = f"Title: {idea.get('title','')}"
    if body:
        txt += f"\nDetails: {body}"
    return txt

def call(payload_text, retries=3):
    data = json.dumps({
        "model": MODEL,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": payload_text}],
        "temperature": 0, "max_tokens": 90,
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(URL, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.load(r)
            return (out["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:
            if attempt == retries - 1:
                return f"__ERROR__ {exc}"
            time.sleep(2 * (attempt + 1))

def main():
    ideas = yaml.safe_load(open("/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml"))["ideas"]
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo = []
    for i in ideas:
        iid = i.get("id")
        if not iid:
            continue
        text = build(i)
        key = hashlib.sha256(text.encode()).hexdigest()[:16]
        if cache.get(iid, {}).get("key") == key and not cache[iid]["just"].startswith("__ERROR__"):
            continue
        todo.append((iid, key, text))
    print(f"{len(ideas)} ideas, {len(todo)} to generate", flush=True)
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "4"))) as ex:
        futs = {ex.submit(call, t): (iid, k) for iid, k, t in todo}
        for fut in cf.as_completed(futs):
            iid, k = futs[fut]
            cache[iid] = {"key": k, "just": fut.result()}
            done += 1
            if done % 20 == 0 or done == len(todo):
                el = time.time() - t0
                print(f"  {done}/{len(todo)}  {el:.0f}s elapsed, "
                      f"{el/done*(len(todo)-done):.0f}s left", flush=True)
                json.dump(cache, open(CACHE, "w"), indent=1)
    json.dump(cache, open(CACHE, "w"), indent=1)
    errs = [k for k, v in cache.items() if v["just"].startswith("__ERROR__")]
    print(f"done. {len(cache)} cached, {len(errs)} errors")

if __name__ == "__main__":
    main()
