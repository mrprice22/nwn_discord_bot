# Autopilot — unattended plan.md loop

Runbook for **autopilot mode** in this repo: an unattended Claude session that works the
[plan.md](plan.md) backlog item by item until it runs out of items or compute. Start it with
the `/autopilot` skill, or by telling a session "follow CLAUDE-autopilot.md".

This repo builds a bot that writes to **two live systems** — a public Discord guild and the
admin's roadmap editor. Neither is a sandbox and neither has an undo. The whole design of this
loop is that autopilot writes *code*, and a human runs anything that touches either one.

## Context economy

- **Files are the only state.** Each iteration must be executable with zero memory of the
  previous one: `plan.md`, `autopilot-wip.md` and git are the entire loop state. Never depend
  on conversation history for what is done or in flight.
- **Fresh context per item.** The orchestrating session does the bookkeeping (select, edit
  `plan.md`, commit) and launches a `general-purpose` subagent — synchronous,
  `run_in_background: false` — to implement each item. Its brief must include the item's `[id]`
  and full text, an instruction to read `CLAUDE.md` (if present) and this file first, the hard
  rules below, and what to report back. Trivial one-liners may be done inline; don't pay a
  subagent spin-up for a typo.

## The loop

### 0. Reconcile

Before picking anything, check for a run cut off mid-item. Read `autopilot-wip.md` and run
`git status --porcelain`.

- `id: none` and a clean tree → nothing to reconcile; go to step 1.
- `stage: shipping` with a `commit:` hash but `plan.md` does not yet show that item `done` →
  the code shipped and a reboot hit between the code commit and the plan commit. Do not redo
  the work; resume at step 5.2 with the recorded hash.
- `stage: implementing` / `test` naming an item, or a dirty tree → resume *that* item, do not
  pick a new one. Inspect what is on disk and either finish it cleanly or take the escape hatch
  (step 4). `git status` before anything destructive; never `reset --hard` or `clean -f`
  without confirming first.

### 1. Select

Re-read `plan.md` fresh — the admin may have edited it, answered a review item, or unblocked
something since the last iteration. Pick one item with status `todo`, smallest and most
self-contained first.

**Before picking, sweep `## Needs human review`:** if an entry that was blocking an item now has
an `**Answer:**` other than `_(unanswered)_`, set that entry to `status: answered`, flip the
blocked item to `todo`, and fold the answer into the item's text so the next iteration needs no
cross-reference. Commit that as its own plan commit before starting work.

An item is only pickable when **every** review entry blocking it is answered. All-or-nothing —
never resume a `blocked` item partially.

### 2. Implement

Run in a fresh subagent (see Context economy). Keep `autopilot-wip.md` live throughout:
overwrite it at the start of the item with the `id`, a `started` timestamp and
`stage: implementing`; append every repo-relative path you create or edit to its `files:` line
(that line is the manifest the safety net is allowed to stage); keep `notes:` a current
one-line summary of what is in flight — it is the only human-readable handoff if the session
dies. Set `stage: test` before step 3 and `stage: shipping` with the `commit:` hash right after
step 5.1.

**Build mechanical, queue judgment.** Autopilot implements what the backlog item already
specifies. Anything that is a choice — a threshold, a message's wording, a schema change to
`nwn_homers_lotr`, which forum tag maps to which group, whether to auto-merge a duplicate —
goes to `## Needs human review` with a **proposed answer written out**, and the item goes
`blocked`. Propose freely; never act on the proposal until it is answered. A held item is
cheap; a bot that mass-posts to a live player forum is not.

Two rules specific to this repo:

- **The planners in `nwnbot/sync.py` stay pure.** They take snapshots and return action lists;
  they never call Discord or the roadmap API. Every new behaviour is a new action type plus a
  table-driven test, not an inline side effect. This is what keeps `plan` (dry-run) honest.
- **`notes` is the admin's field.** The bot appends to the internal, append-only `comments`
  list. It never writes `notes` or `impl_notes` on an existing idea. If an item seems to
  require rewriting `notes`, that is a review question, not an implementation detail.

### 3. Test

```
python -m pytest -q
```

Must pass before shipping. On failure: fix and re-run; if unfixable, take the escape hatch.
For an item that touches `nwnbot/roadmap.py`, also run the integration test against a
throwaway editor instance (a git clone of `nwn_homers_lotr` into `/tmp`, a fixture
`roadmap.yaml`, `$ROADMAP_AUTH_DB` pointed at a temp sqlite, the editor on port 8799) — see
plan.md's verification notes. Never point that test at the live editor or the live tunnel.

### 4. Escape hatches

- **Needs a decision** → append the question(s) to `## Needs human review` with a dated entry,
  a proposed answer and `status: open`; set the backlog item to `blocked`; commit whatever
  partial work is safe (it must still pass `pytest`); go back to step 1.
- **Too big for one iteration** → append a dated progress line to the item, leave it `wip`,
  commit safe partial work, continue it next iteration. After ~2 stalled iterations, treat the
  scope problem itself as a review question and set it `blocked`.

### 5. Ship

1. **Commit the code** on `main` (this repo commits straight to main — no branches, no PRs).
   Message: `<item id>: <what changed>`. Then set `autopilot-wip.md` to `stage: shipping` with
   the hash.
2. **Update `plan.md`**: set the item's status to `done`, and append one line to `## Log`:
   `` `[id]` · YYYY-MM-DD · <hash> · <one-line summary> ``. Use today's real date — check it,
   don't guess. If the work revealed follow-up work, add it as a new backlog item rather than
   widening a finished one.
3. **Plan commit**: commit `plan.md` on its own, message `plan: <id> done`. Then reset
   `autopilot-wip.md` to `id: none` (it is gitignored, so this is a plain file write, not part
   of any commit).
4. **Push** to `origin/main` after each item so nothing sits unpushed.

### 6. Loop

Back to step 1. Work is synchronous, so chain iterations directly in one turn. Use
`ScheduleWakeup` only as a long fallback (~1800s) if genuinely waiting on something, and end
with `stop: true` at the stop condition.

## Hard rules — never do these

- **Never run the bot against the live Discord guild or the live roadmap.** No
  `python -m nwnbot apply`, `backfill --yes`, or `serve` against real credentials; no request to
  `roadmap.homerslotr.com` or `127.0.0.1:8765`. `doctor` and `plan` against live systems are
  the admin's to run. Tests use fakes and a throwaway editor instance on port 8799.
- **Never commit `.env`, `state.db`, `players.json`, or `backfill-plan.md`.** They are
  gitignored; if one shows up staged, unstage it and say so.
- **Never edit `/var/home/james/GIT/nwn_homers_lotr`** except for the exact diff described in
  `[b2-roadmap-schema]`, and only once review item `[r1]` is answered. Cloning it read-only into
  `/tmp` for the integration test is fine.
- **Never write `status: awarded`, `status: implemented`, `status: manual`, or
  `merit_awarded`** anywhere — in code paths, in tests against a live system, or by hand.
  Shipping and merit are the admin's call. (Fixture data in `tests/` may of course contain
  them.)
- **Never auto-merge a duplicate outside the rules in `[b9-dupes]`**, and never mint a
  `dupe_of` row pointing at another `dupe_of` row.
- **Never add a name to the roadmap's `players:` list** — an unrecognised Discord author is a
  review item.
- **Never answer a `## Needs human review` entry yourself**, including one you wrote. Proposing
  an answer is the job; marking it `answered` is not.
- **Never delete or rewrite `## Log` entries.** It is append-only history.
- **Never hard-code a token, password, channel id or user id** in a committed file.

## Stopping

Stop when every backlog item is `done` or `blocked` (nothing left `todo`), or compute runs out.
Running out of *unblocked* items is a legitimate stop: it means the review queue needs the
admin. On stop, make sure the tree is committed and pushed, then report a session summary in
your final message — items shipped, items blocked, and the count of open review entries. The
summary is not written to any file.

## Session-boundary safety net

`autopilot-wip.md` (repo root, gitignored — local machine state) backs up a session killed with
no chance to clean up. `.claude/settings.json` wires `PreCompact` and `SessionEnd` to
`bin/autopilot-safety-commit`, which acts **only** when `autopilot-wip.md` names an active item
whose `files:` paths actually changed, stages exactly those paths, and commits. It is a no-op in
ordinary interactive sessions and never runs a blanket `git add -A`. It cannot write handoff
notes — that needs judgment — so the live `notes:` line you maintain in step 2 is the real
handoff.
