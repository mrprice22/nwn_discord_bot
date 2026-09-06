# Future work — LLM semantic matching and the `justification` field

**Status: designed, deliberately not built.** Deferred 2026-09-05 in favour of the stdlib
token scorer that `[b9-dupes]` ships. This file is the durable record of the design so it
survives without being re-derived. Revisit only if the token scorer is visibly missing
duplicates in practice.

Nothing here changes the policy `[r6]` settled: **the bot never writes `dupe_of`.** Everything
below improves *which* duplicates get proposed and how well a human can judge a proposal. A
duplicate still becomes real only when a DM or admin sets `dupe_of` in the roadmap editor.

---

## Measured: what the token scorer actually achieves

Added 2026-09-05, **after** `[b9-dupes]` shipped and was calibrated against the real
`roadmap.yaml`. This is the evidence, and it is stronger than the argument this document
originally made from first principles.

`roadmap.yaml` carries **five `dupe_of` rows** — real duplicates, confirmed by hand. They are
ground truth. Scored with the shipped token scorer (`title_weight` 0.3):

| duplicate | canonical | score | rank of the true match |
|---|---|---|---|
| `delevel-mcgondy` | `relevel-option` | 0.508 | **1st** |
| `wiki-top-killers` | `wiki-kill-counts` | 0.211 | **1st** |
| `teleport-last-eru` | `teleport-expansion` | 0.200 | 4th |
| `forge-limits-progressive` | `achievements` | 0.125 | 19th |
| `high-end-mat-crafting` | `cnr-crafting` | 0.095 | 93rd |

Against that, scoring all 404 ideas as if each were a fresh thread — where every top-1 match is
a false positive by construction:

| threshold | new threads that would file an entry |
|---|---|
| 0.20 | 53% |
| 0.30 | 18% |
| 0.50 | 10% |

**The two distributions overlap almost completely.** To catch the second-best real duplicate
(0.211) you must accept a threshold that fires on half of all new threads. At a usable 10%
false-positive rate you catch exactly one of the five.

Roughly: **20% recall at 10% precision-ish cost, or 40% recall at coin-flip noise.** And
`[r6]`'s originally proposed 0.55 / 0.85 would have found **none of the five**.

Two structural reasons, both visible in the data:

1. **The real duplicates here are paraphrases.** "Rest-menu teleport back to where you last used
   the Well-of-Eru port" vs "Expand rest-menu teleports (earned via quests...)". Same subject,
   almost no shared words. This is not the exception on this corpus — it is the norm, because
   the admin merges things that are conceptually the same, not lexically the same.
2. **The strongest lexical signals are deliberately-distinct siblings.** "Prestige quest: Pale
   Master (L11+)" vs "Prestige quest: Weapon Master (L13+)" scores **0.83** — higher than every
   real duplicate in the corpus. The roadmap uses title templates for whole families of items,
   and shared boilerplate is exactly what `difflib` rewards.

This is why `cfg.DUPE_POST_IN_THREAD` ships **off**: a matcher that is right about one duplicate
in five has not earned a player-visible claim. The review queue still receives every candidate,
so the admin loses nothing.

**The conclusion this measurement forces:** semantic matching is not a "nice to have down the
road" for this corpus. It is the only thing that moves recall off ~20%. Everything below is the
design for it.

---

## The problem it solves

Token overlap is good at one thing and blind to another.

It **catches restatements** — the same sentence reworded:

> "smith can disenchant negative abilities" ≈ "disenchanting negative abilities at the smith"

It **completely misses paraphrase** — the same issue described with different words:

> "my henchman won't follow me through a transition" ≈ "companions get left behind when you
> use a teleporter"

Those two share almost no tokens. No threshold tuning finds them, because there is no number at
which lexical overlap distinguishes that pair from noise. It needs semantics, which is the one
thing an embedding gives you that `difflib` cannot.

## Shape: embeddings for recall, chat model for precision

Two stages, because they fail differently and one is far cheaper than the other.

1. **Recall.** Embed every non-`dupe_of` idea once. Brute-force cosine similarity of a new
   thread against all of them; take the top ~15.
2. **Precision.** A chat model judges that shortlist and returns a verdict *plus one sentence
   of reasoning*.

The reasoning sentence is the part that makes a proposal actionable. A number alone does not
tell you whether to accept a suggestion; a sentence does. Instead of

> Possible duplicate of *Companions get left behind on transitions* (0.71)

the review entry reads

> Possible duplicate of *Companions get left behind on transitions* — both describe the
> henchman failing to follow through an area transition.

That sentence is display-only. It is never matched on, never scored, never written to the idea.

## Footprint — measured, not estimated

Against the real `roadmap.yaml` (404 non-`dupe_of` ideas):

| | present on | mean | median | p90 | max |
|---|---|---|---|---|---|
| `title` | 404/404 | 58 ch | 51 | — | 170 |
| `notes` (tags stripped) | 334/404 | 479 ch | 317 | 1,028 | 4,347 |
| `impl_notes` | 172/404 | 2,547 ch | 1,790 | 5,048 | 14,740 |

Title + notes across the whole corpus is **183,627 chars ≈ 46k tokens**.

**Index size tracks vector count, not text length.** One embedding per idea is the same 1024
floats whether it was fed 51 characters or 1,028:

- title only — 404 × 1024 × 4 B = **1.6 MB**
- title + notes, one vector each — **1.6 MB**, identical
- with the long tail chunked (~60 items) — **~2.2 MB**
- at 768 dim, 1.2 MB; at 384 dim, 600 KB

It is a flat file loaded into a list and brute-forced in microseconds. **No vector database, no
index structure, no new runtime dependency.** One-time embedding cost is ~46k tokens for the
whole corpus; after that you re-embed an item only when its title or notes hash changes, which
is a handful per week.

So there is no footprint argument for embedding titles alone. Include the notes.

**The real cost is dilution.** A 1,000-token note averaged into a single vector washes out the
topic — a bug about henchman pathing whose notes spend 800 tokens on a repro table and a
changelog embeds as "generic long roadmap item". Two cheap mitigations: cap the embedded text
(title + first ~400 chars of notes captures the median item whole), or chunk the p90 tail and
take the best-matching chunk. Cap first; chunk only if calibration shows the long items
misbehaving.

**Exclude `impl_notes`** — 2,547 chars mean, and it describes how a fix was built rather than
what a player reported. It would dominate the vector and match items by *technology* instead of
by *symptom*. The token scorer already excludes it for the same reason.

## The `justification` field

The corpus has an asymmetry that embeddings will trip on: **rambly player-written Discord posts
on one side, polished admin-written release notes on the other.** Those register in very
different voices, and voice is a large part of what an embedding picks up — two texts can end up
near each other for sounding alike rather than for meaning alike.

The fix is to normalize both sides to the same terse *symptom + subsystem* restatement and embed
**that** instead of the raw text.

### Schema

A new `justification` field on the idea, added to `IDEA_FIELDS`
(`nwn_homers_lotr/bin/gen-roadmap.py:160`) and `FIELD_ORDER`
(`nwn_homers_lotr/bin/roadmap-editor.py:77`). **Change both together** — they are asserted equal
at import time. Ship it as its own standalone, revertible commit in `nwn_homers_lotr`, the same
discipline `[b2-roadmap-schema]` and `[b11-code-tags]` followed.

It is **displayed in the roadmap editor and human-editable**. This is a first-class field, not a
hidden cache — the admin can read the bot's understanding of an item and correct it.

### The human-edit-wins rule

**Once a human edits `justification`, the LLM must never overwrite it.** This is the whole
reason the field can safely be public and editable.

Mechanism: alongside the value, store a hash of the text *the bot generated*. On regeneration,
hash the current value and compare:

- **matches** — still bot-owned, safe to regenerate.
- **does not match** — a human has touched it. Leave it alone, permanently, and record the
  decision so the rule survives a lost database.

This is the same shape as `store.DECISION_DUPE_REMOVED`: an admin's edit is a decision the bot
must not undo. A `regenerated_at` stamp lets you find stale entries without weakening the rule.

Where the hash lives is a real choice:

- **`justification_src` on the idea** — survives a lost `state.db`, visible in `roadmap.yaml`,
  costs a second schema field.
- **In the bot's own `state.db`** — keeps the roadmap schema to one field, but a wiped database
  makes every human edit look bot-owned again. If you take this route, the recovery rule must be
  "unknown provenance means leave it alone", never "unknown means regenerate".

Regenerate only when `title` or `notes` changed **and** the field is still bot-owned.

### Poisoning, and the mitigation

A restatement is a lossy summary, so a hallucinated one poisons that item's recall permanently
and invisibly — it will simply stop matching things it should match, and nothing surfaces that.

Mitigation: **keep the raw-text vector as well as the justification vector, and take the max of
the two similarities.** 3.2 MB instead of 1.6 MB, which is nothing, and a bad restatement
degrades recall to baseline rather than below it.

## Purity: where the call goes

An LLM call is I/O, and `plan_discord_to_roadmap()` is strictly pure and synchronous
(`nwnbot/sync.py:6-12`), enforced by the planner-purity tests and by `simulate`'s fixed-point
property. **The call cannot go inside the planner.**

Pre-compute it upstream in `SyncEngine.cycle` (`nwnbot/bot.py:196-220`) — already `async`,
already awaiting `self.source.snapshots()` — and hand the planner a
`Mapping[thread_id, DupeVerdict]` as a field on `PlanContext`. That is exactly the rule stated
at `nwnbot/sync.py:29-33`: *configuration is an input, not an import*. The planner stays pure,
deterministic and offline; the tests never open a socket.

**Cache verdicts in `state.db`**, keyed on `(thread content hash, candidate-set hash)`. This
matters for more than cost: an LLM is a nondeterministic oracle, and caching makes a replay
deterministic and free, which is what keeps the fixed-point tests honest. It also bounds the
blast radius of an outage to the threads that arrived during it — which you can then re-examine
deliberately, because the cache tells you which ones they were.

## Client

Mirror `RoadmapClient` (`nwnbot/roadmap.py:359-566`), which already solved these problems:

- **Injectable transport** (`roadmap.py:370-380`) — accept any object with aiohttp's
  `request(method, url, *, json=None, headers=None)` async-context-manager shape, so tests
  inject a fake and never open a socket.
- **Lazy, owned-vs-borrowed session** (`roadmap.py:403-418`), with `aiohttp` imported inside
  `__aenter__` so an injected transport needs no aiohttp at all.
- **Wrap the API key in `_Secret`** (`roadmap.py:165-187`) — `__repr__`/`__str__` return
  `<hidden>` and `.reveal()` is the single greppable exit.
- **Add an explicit `aiohttp.ClientTimeout`.** There is none anywhere in this repo today, and
  the LLM host is the first dependency that can hang. Do not mirror the roadmap client here.

## Degradation

The host is `http://10.42.0.83:8080`, OpenAI-compatible `/v1`, on an IP that can move.

- Short timeout, one retry, then **fall back to the token scorer** and say so in the run
  summary. The bot never blocks on the LLM and a dead box never stops Discord↔roadmap sync.
- Auto-merge is not a concern, because the bot does not auto-merge. If that policy is ever
  revisited, an outage must not be able to widen the bot's authority: any auto-action would need
  the LLM's agreement, so losing the LLM means losing the action, not falling back to acting on
  lexical score alone.
- **`doctor` probes `GET /v1/models`** and prints which model answered. A moved IP is then one
  env-var edit plus one command, not a debugging session.
- **Pin the model name in config.** A swapped model then shows up as a visible config change
  rather than a silent behaviour change.

New env vars, mirroring the existing `ENV_*` block in `nwnbot/config.py:108-119`:
`NWNBOT_LLM_BASE_URL`, `NWNBOT_LLM_MODEL`, `NWNBOT_LLM_EMBED_MODEL`, `NWNBOT_LLM_API_KEY`.

## Operational note: two models, two ports

Embeddings need an **embedding model**. A chat MoE will not serve `/v1/embeddings` usefully, so
this is not something the existing box does for free.

- **Two `llama-server` processes on two ports** is the simplest thing that works. A 0.6B
  embedding model is ~1.2 GB and can sit resident beside the chat model permanently.
- **`llama-swap`** gives you one port but swaps on demand, so every embed call may evict the
  chat model and vice versa — the wrong trade for a workload that alternates constantly.

Candidate embedding models, all small and all fine: `Qwen3-Embedding-0.6B` (1024 dim, pairs
naturally with a Qwen chat model), `bge-m3` (1024), `nomic-embed-text-v1.5` (768).

## Order of work, if this is ever picked up

1. Run `python -m nwnbot dupes --calibrate` first and look at what the token scorer actually
   misses. If the misses are restatements, tune the existing weights — this whole document is
   the wrong tool. Only paraphrase misses justify it.
2. Embeddings over **raw** title + notes, scored alongside the token scorer, max of the two.
   Measure against the same calibration corpus before adding anything else.
3. The chat-model judge over the shortlist, verdict cached in `state.db`.
4. The `justification` field, only if step 2 shows the voice asymmetry is really costing recall.
   It is the most machinery and the most schema risk of the four, and it is the only one that
   touches `nwn_homers_lotr`.
