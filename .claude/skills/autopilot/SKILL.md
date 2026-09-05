---
name: autopilot
description: Run the unattended plan.md autopilot loop for the Discord/roadmap sync bot — pick a backlog item, implement it in a fresh subagent, test, commit, tick plan.md, and repeat until the backlog is done or blocked. Use when the user says "autopilot", "run the plan", or "work the backlog unattended".
---

# Autopilot

Read **CLAUDE-autopilot.md** (repo root) and execute its loop exactly. The runbook is
authoritative — read it in full before starting. Summary of what you're signing up for:

0. **Reconcile first**: check `autopilot-wip.md` + `git status` for a previous run cut off
   mid-item before picking anything new.
1. Sweep `## Needs human review` in `plan.md` for newly-answered entries, unblock the items they
   were gating, and commit that as its own plan commit.
2. Pick one `todo` item — smallest and most self-contained first. An item is pickable only when
   *every* review entry blocking it is answered.
3. Implement it **in a fresh `general-purpose` subagent** (fresh context per item; inline only
   for trivial one-liners), keeping `autopilot-wip.md` live with the item id, `stage`, and the
   `files:` manifest the safety-net hook is allowed to stage.
   - **Build mechanical, queue judgment.** Thresholds, message wording, tag→group mappings,
     schema changes to `nwn_homers_lotr`, whether to auto-merge a duplicate — all of these go to
     `## Needs human review` with your proposed answer written out, and the item goes `blocked`.
     Never act on your own proposal.
   - The planners in `nwnbot/sync.py` stay **pure** — snapshots in, action lists out, no I/O.
   - The bot appends to the roadmap's internal `comments` list; it never writes `notes`.
4. `python -m pytest -q` must pass. Roadmap-client work also runs the integration test against a
   throwaway editor on port 8799 (a `/tmp` clone + `$ROADMAP_AUTH_DB`), never the live one.
5. Ship: code commit on `main` → set the item `done` and append to `## Log` with today's real
   date and the hash → separate `plan.md` commit → reset `autopilot-wip.md` to `id: none` →
   push to `origin/main`.
6. Repeat until nothing is left `todo`, then report a session summary in your final message.

Honor every **hard rule** in the runbook. The load-bearing ones: never run the bot against the
live Discord guild or the live roadmap (no `apply`, `backfill --yes`, or `serve`); never touch
`nwn_homers_lotr` outside the approved `[b2-roadmap-schema]` diff; never write `status: awarded`
/ `implemented` / `manual` or `merit_awarded`; never answer a review entry yourself; never
commit `.env`, `state.db` or `players.json`.

Pacing: work is synchronous, so chain iterations directly in one turn where possible. Use
ScheduleWakeup only as a long fallback (~1800s) if genuinely waiting, and end the loop with
`stop: true` when the runbook's stop condition is met.
