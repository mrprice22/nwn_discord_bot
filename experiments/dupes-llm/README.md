# Experiment: LLM justifications for duplicate detection

**Throwaway research code, not part of the `nwnbot` package.** Nothing here is imported by the
bot, covered by the test suite, or run by `serve`. It exists so the numbers in
[`../../future-llm-dupe-matching.md`](../../future-llm-dupe-matching.md) can be reproduced or
challenged rather than taken on trust.

Run 2026-09-05 against a local `Qwen3.6-35B-A3B-Q4_K_M` on llama.cpp, OpenAI-compatible `/v1`.

| file | what it does |
|---|---|
| `gen_just.py` | Restates every roadmap idea as a canonical one-liner via the LLM. Cached and resumable — re-running only regenerates items whose text changed. |
| `justifications.json` | The 409 restatements from that run. Keyed by idea id, with a hash of the input so a stale entry is visible. |
| `evaluate.py` | Pure token matching over raw text vs justifications vs both. Reports rank of the true canonical for the five known `dupe_of` pairs, and the false-positive curve. |
| `margin.py` | Whether top-1 minus top-2 margin, or their ratio, separates real matches from noise. (It does not.) |
| `judge.py` | The two-stage pipeline: justification shortlist, then an LLM judge over the shortlist. |

## Reproducing

```bash
export WORKERS=6
.venv/bin/python experiments/dupes-llm/gen_just.py     # ~20 min for 409 ideas
cd experiments/dupes-llm
../../.venv/bin/python evaluate.py
../../.venv/bin/python judge.py
```

The endpoint and model id are constants at the top of `gen_just.py` — the box's IP moves, so
expect to edit `URL`. **`enable_thinking: false` is not optional**: it is a reasoning model and
without it the whole token budget goes to `reasoning_content` and `content` comes back empty.

## The short version of what it found

Justifications move top-1 recall from 2/5 to 3/5 and rescue the hardest paraphrase from rank #88
to #1 — but scores compress, so nothing thresholds cleanly. The limiting factor turned out to be
the ground truth: five `dupe_of` rows is too small and too soft a target, and several of the
judge's "false positives" look like genuine unmerged duplicates. Full write-up one directory up.
