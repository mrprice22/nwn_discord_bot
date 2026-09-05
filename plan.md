# nwn_discord_bot — implementation plan

**Backlog and loop state for autopilot.** This file is the only durable state the loop has;
it must be executable with zero memory of the previous iteration. See
[CLAUDE-autopilot.md](CLAUDE-autopilot.md) for the runbook.

---

## Context

The two Discord channels `#bugs` and `#feature-requests` are now **forum** channels with 12
tags that correspond one-to-one with the 12 `groups:` in the roadmap editor
(`/var/home/james/GIT/nwn_homers_lotr/roadmap.yaml`). Today the pipeline between them is a
human: the admin reads Discord, hand-copies the conversation into the editor's rich-text
`notes` box, invents an id, picks a group and credits a player. Around 15 items in
`roadmap.yaml` still carry pasted Discord DOM (`discord.com/assets/*.svg` emoji and all) as
evidence. Nothing flows back — a player who reported a bug never learns it shipped, and threads
stay open forever.

**Goal:** keep the two forums and the roadmap in sync in both directions. New forum posts become
roadmap ideas; open roadmap ideas get forum threads; replies and status changes propagate; a
thread is closed when its idea's merit is paid.

The existing `scraper.py` (a one-shot `TextChannel` history dumper) predates the forum migration
and is superseded.

## Integration facts (verified 2026-09-05 — do not re-derive)

- Roadmap editor is `bin/roadmap-editor.py` in `nwn_homers_lotr`: a stdlib
  `ThreadingHTTPServer` on `127.0.0.1:8765`, published through a cloudflared tunnel at
  **`https://roadmap.homerslotr.com`**. The bot talks to it over the tunnel, so it can run
  anywhere.
- `GET /api/data` returns `{ideas, vocab, environments, base_hashes, base_vocab, version, me}`.
- `POST /api/save` takes the **whole** ideas array plus `base_version` and `base_hashes`
  echoed back verbatim. `merge_ideas()` (`roadmap-editor.py:289`) does a three-way merge and
  only refuses when the *same* idea changed on both sides. Every POST holds a flock.
  Do not reimplement this, and never hand-edit `roadmap.yaml`.
- `POST /api/idea-comment` appends `{author, date, text}` to the item's append-only, internal
  `comments` list, stamping author and date server-side. Never rendered publicly.
- Auth: username + password → `roadmap_session` cookie. No token auth exists. `_csrf_ok()`
  (line 2861) only requires `Content-Type: application/json`, so a non-browser client passes.
- Item schema: `IDEA_FIELDS` (`bin/gen-roadmap.py:160`) and `FIELD_ORDER`
  (`bin/roadmap-editor.py:77`) are asserted equal at import — **change both together**.
- The 12 group ids: `forge`, `combat-classes`, `bosses`, `progression`, `travel`, `banking`,
  `wiki-tools`, `quests-areas`, `items-gear`, `meaningwave`, `economy`, `qol`.
- 10 statuses (`bin/gen-roadmap.py:73`). **There is no `closed`.** Terminal states are
  `awarded` and `unlikely`. `merit_awarded: true` is a *separate* boolean recording that the
  game merit DB was really credited; status can bounce, the boolean cannot. Close threads on
  the boolean.
- Merit value comes from `type`: Defect 1, Enhancement 2, Exploit 3.
- **Duplicates are already modelled**: there is no vote or `+1` counter. A second submitter of
  the same idea gets their **own idea row** carrying `dupe_of: <canonical-id>` and their own
  `player:`, which is how they still earn merit. `gen-roadmap.py` folds the dupe row into the
  canonical item's card. So "merge" means *mint a dupe row*, never *edit the original*.
- **Tests run from `.venv/`.** System Python is 3.14.7 and has `aiohttp`, `PyYAML` and
  `discord.py` but *not* `pytest`. The repo carries a gitignored `.venv`
  (`python -m venv --system-site-packages .venv`); run the suite as
  `.venv/bin/python -m pytest -q`, not bare `python -m pytest`.
- `$ROADMAP_AUTH_DB` overrides the account DB path — this is what makes a real end-to-end
  integration test possible against a throwaway editor instance.

## Decisions taken (settled — do not re-litigate)

| Question | Decision |
|---|---|
| How the bot writes to the roadmap | HTTP API over the Cloudflare tunnel (`ROADMAP_BASE_URL`), falling back to `http://127.0.0.1:8765` when colocated |
| Where the thread↔idea link lives | A new `discord:` field on the idea, added to both `IDEA_FIELDS` and `FIELD_ORDER` |
| Initial editor→Discord backfill | Open, non-hidden items only; skip `hidden`, `awarded`, `unlikely`, `dupe_of`. Report first, human approves, then a rate-limited batch |
| Autopilot blast radius | Code only. Never writes to the live guild or the live roadmap |

---

## Backlog

Each item: `[id]` · status `todo` | `wip` | `blocked` | `done`. Work one end to end per
iteration. Acceptance is the line that says how you know it is finished.

### `[b1-scaffold]` — status: done
Delete `scraper.py`. Create the `nwnbot/` package (`__init__`, `config`, `store`, `roadmap`,
`render`, `forum`, `sync`, `cli`, `bot`) and `tests/`. Add `aiohttp`, `PyYAML`, `pytest`,
`pytest-asyncio` to `requirements.txt`. Extend `.env.example` with `ROADMAP_BASE_URL`,
`ROADMAP_USER`, `ROADMAP_PASSWORD`, `DISCORD_BUGS_FORUM_ID`, `DISCORD_FEATURES_FORUM_ID`,
`NWNBOT_DB`, `NWNBOT_DRY_RUN=1`. Add `state.db`, `players.json`, `backfill-plan.md` to
`.gitignore`. Make the repo's first commit.
**Acceptance:** `python -c "import nwnbot"` succeeds; `pytest` collects zero failures; `git log`
shows one commit; `git status` clean.

### `[b2-roadmap-schema]` — status: blocked
*Blocked: touches `nwn_homers_lotr`, a service the admin uses daily. Needs human sign-off —
see review item `r1`.*
In `nwn_homers_lotr`, one standalone commit: add `"discord"` to `IDEA_FIELDS`
(`bin/gen-roadmap.py:160`) and `FIELD_ORDER` (`bin/roadmap-editor.py:77`, after `commit`); add
`"bot": {"view", "edit", "uat"}` to `ROLES` (`bin/roadmap_auth.py:84`) plus a `ROLE_LABELS`
entry and a `BOT_FORBIDDEN` assertion in `bin/roadmap-auth-selftest.py` mirroring
`TESTER_FORBIDDEN`.
**Acceptance:** `python3 bin/roadmap-lint.py` clean, `bin/roadmap-auth-selftest.py` passes, the
editor starts without the import-time drift warning.

### `[b3-roadmap-client]` — status: todo
`nwnbot/roadmap.py`: async `aiohttp` `RoadmapClient` with `login`, `fetch`, `save`, `comment`,
`new_idea`. `save()` re-fetches, applies mutations, and posts `base_version` + the server's own
`base_hashes` verbatim — never compute fingerprints locally. On conflict, re-fetch and retry
once; on a second conflict, queue for review rather than forcing. Encode the hard rules as
assertions, not comments: never write `status: awarded|implemented|manual`, never write
`merit_awarded`, never touch `meta`/`groups`/`players`/`epics`/`redemption`/`housing`, never
add a name to `players:`, always send `Content-Type: application/json`.
**Acceptance:** unit tests over a fake HTTP transport cover login, save, retry-on-conflict and
every assertion; each forbidden write raises.

### `[b4-render]` — status: done
`nwnbot/render.py`: `md_to_html()` (the `<div>`-per-line shape the editor's rich-text box
produces; bold/italic/code/links/lists only, everything else escaped, never `<script>`/`<style>`)
and `html_to_md()` (strip Discord DOM chrome, collapse `<div><span>` nesting, keep links and
images as URLs). `cdn.discordapp.com` links are signed and expire — store them as plain links
and say in the item that they may 404; do not rehost. Truncate Discord-bound text at 4000 chars
with a link back to the editor.
**Acceptance:** round-trip tests assert the *second* round-trip is a fixed point (the first is
allowed to normalize); a real pasted-Discord `notes` blob from `roadmap.yaml` survives
`html_to_md` without markup leaking through.

### `[b5-config]` — status: blocked
*Blocked: needs the real forum tag names and channel ids — see review items `r2` and `r3`.*
`nwnbot/config.py`: env loading plus the literal tag-name → group-id dict, validated at startup
against `vocab` from `/api/data` and against the forum's `available_tags`; fail loudly when
either side has drifted. Forum → type: bugs ⇒ `Defect`, feature requests ⇒ `Enhancement`.
Player identity map `players.json` (`discord_user_id` → roadmap player name), seeded by parsing
the parentheticals already in `players:` (`"Sync (Shync)"`, `"Balendin (Balendin_2222)"`);
every unmatched author is queued for review, never auto-added.
**Acceptance:** `doctor` reports all 12 tags mapped to all 12 groups, and exits non-zero on a
deliberately broken mapping.

### `[b6-sync]` — status: todo
`nwnbot/sync.py`: `plan_discord_to_roadmap()` and `plan_roadmap_to_discord()`, both **pure**
functions from (roadmap snapshot, forum snapshot, store) to a list of planned actions. Nothing
executes inside them — that is what makes dry-run honest and the tests cheap.
- Discord → roadmap: new thread ⇒ create idea (`hidden: true`, id slugified per the editor's
  own `slugifyId`/`shortenId` rules at `roadmap-editor.py:4537`, `group` from the tag, `type`
  from the forum, `player` from the identity map, `discord: {…}`); new replies ⇒ append a
  `comment`, **never** an edit to `notes` (`notes` is the admin's player-facing release note and
  is only ever written by a human); tag changed ⇒ update `group`; thread renamed ⇒ queue for
  review, since the id is an anchor target for cross-links.
- Roadmap → Discord: open non-hidden non-`dupe_of` idea with no thread ⇒ create one, tagged from
  `group`; `status` changed ⇒ post; `merit_awarded: true` ⇒ post the merit award and
  `thread.edit(archived=True, locked=True)`; `status: unlikely` ⇒ post and archive **without**
  locking.
- Loop prevention, three layers: skip messages authored by the bot's own user id; a content hash
  per (idea, field) that skips unchanged values; and a per-run action cap (default 25) that
  aborts and reports rather than executing a runaway batch.
**Acceptance:** table-driven tests cover every branch above; a snapshot that is already in sync
produces an empty action list; feeding a planner's own output back in is a no-op.

### `[b7-cli-runtime]` — status: todo
`nwnbot/cli.py`: `doctor`, `plan`, `apply`, `backfill`, `serve`. `apply` refuses without `--yes`
*and* `NWNBOT_DRY_RUN=0`. `nwnbot/bot.py`: `discord.Client` with `message_content` + `guilds`
intents handling `on_thread_create`, `on_message`, `on_raw_thread_update`, plus a 15-minute full
reconcile so the event path and the poll path cannot diverge (both funnel through the same
planner). Ship `systemd/nwnbot.service` as a user unit, `Restart=on-failure`,
`EnvironmentFile=`, deliberately **not** enabled by default — arming it is a decision, with a
header comment saying so (mirror `nwn_homers_lotr/systemd/llm-autopilot.service`).
**Acceptance:** `python -m nwnbot plan` against fakes prints an action list and writes nothing;
`apply` without `--yes` exits non-zero; the unit file passes `systemd-analyze verify`.

### `[b9-dupes]` — status: todo
Duplicate detection for ideas raised more than once in Discord. Runs inside
`plan_discord_to_roadmap()` **before** the create-idea branch, and never merges on its own —
a wrong merge silently steals a player's merit credit, so every match is a proposal.

- **Score** a new thread against every non-`dupe_of` idea (~428 today, so an O(n) pass per new
  thread is fine — no index needed). Combine `difflib.SequenceMatcher` on normalized titles
  with token-set overlap over title + first post, after stripping stopwords and the
  group/tag word itself. Stdlib only; do not add a fuzzy-match dependency for this.
- **Three bands**, both thresholds in `config.py` so they can be tuned without a code change:
  - below the low threshold ⇒ ordinary new idea, no mention of duplicates;
  - between ⇒ create the idea as normal, and additionally post one "possible duplicate of
    *<title>*" line in the thread with a link to `#idea-<id>`, plus a review-queue entry. The
    idea is still created — a false positive must never swallow a real report;
  - above the high threshold ⇒ still create the idea, but as a **dupe row**: `dupe_of:
    <canonical-id>`, its own `player:`, `hidden: true`, and the thread linked to it. Post in the
    thread that it has been linked to the existing item, and append a `comment` on the
    **canonical** idea naming the new reporter and the thread URL, so the admin sees the extra
    demand where they actually work.
- **Never** collapse two threads into one Discord-side, and never archive the newer thread —
  the reporter keeps their thread and their credit. Closing follows the canonical item's
  `merit_awarded`, so `[b6-sync]`'s close path must follow `dupe_of` to find the item whose
  merit governs the thread.
- **Never** create a dupe row pointing at another dupe row: resolve `dupe_of` transitively to
  the canonical id first, and treat a cycle as a review item rather than an exception.
- The admin can undo either outcome in the editor; the bot must tolerate a `dupe_of` it did not
  write being removed, and must not re-add it (record the decision in the store).

**Acceptance:** table-driven tests over real title pairs from `roadmap.yaml` — the known
duplicate clusters (e.g. the `smith can disenchant negative abilities` family) score above the
high threshold, and unrelated items in the same group score below the low one. A dupe row
created by the planner passes `roadmap-lint.py`, and re-running the planner on the result
produces no further actions.

### `[b10-wording]` — status: blocked
*Blocked: two player-visible strings are the admin's to word — see review item `r8`.*
Replace the two `PROVISIONAL WORDING` placeholders in `nwnbot/render.py` with the answers to
`[r8]`: the truncation marker (currently a bare `…` plus the editor URL) and `CDN_EXPIRY_NOTE`.
Both are single constants; the surrounding logic and its tests are already shipped and settled.
**Acceptance:** both `PROVISIONAL WORDING` comments are gone, the strings match `[r8]`'s answer
verbatim, and `pytest` still passes.

### `[b8-backfill]` — status: blocked
*Blocked: human-gated by design; the batch must not run unattended. See review item `r4`.*
`backfill` runs `plan_roadmap_to_discord()` over open, non-hidden, non-`dupe_of` items, writes
`backfill-plan.md` grouped by tag with a total, and stops. Re-running with `--yes` executes at
≥2s between thread creations with exponential backoff on 429, checkpointing each new thread id
into its roadmap item immediately so an interruption resumes rather than double-posts.
**Acceptance:** against fakes, a simulated interruption halfway through creates no duplicates on
resume.

---

## Needs human review

Autopilot appends here and sets the blocking item to `blocked`. Never decide one of these
yourself. Each entry is dated, states the question, and proposes an answer so it can be
approved or adjusted in one line.

### `[r1]` 2026-09-05 — Approve the `nwn_homers_lotr` schema change? — status: open
`[b2-roadmap-schema]` edits the roadmap editor, a service you use daily. It adds a `discord`
field to `IDEA_FIELDS` + `FIELD_ORDER` and a `bot` role to `ROLES`.
**Proposed:** approve as a single standalone commit, reviewed and merged before any bot code
lands, so it can be reverted independently. The `bot` role is `{view, edit, uat}` — no
`promote_shipped`, `merit`, `publish`, `audit_view` or `merit_view`, so the bot cannot ship an
item or pay merit even if it tries.
**Answer:** _(unanswered)_

### `[r2]` 2026-09-05 — The 12 forum tag names and the two forum channel ids — status: open
Needed for `[b5-config]`; not discoverable from either repo.
**Proposed:** paste the output of a `doctor --dump-tags` run (or the tag list from the forum
settings) and the two channel ids into `.env` / `config.py`. If a tag name matches a group
`title` closely enough the mapping can be seeded automatically, but it must be confirmed by
hand once.
**Answer:** _(unanswered)_

### `[r3]` 2026-09-05 — Where do `Exploit` items come from? — status: open
`type: Exploit` is worth 3 merit but neither forum implies it: bugs ⇒ `Defect`, feature requests
⇒ `Enhancement`.
**Proposed:** add an `Exploit` tag to the bugs forum and let it override the type. Alternative:
leave `Exploit` as an admin-only reclassification in the editor, and have the bot never set it.
**Answer:** _(unanswered)_

### `[r4]` 2026-09-05 — Backfill approval gate — status: open
`[b8-backfill]` would create ~100+ forum threads in one run.
**Proposed:** autopilot may implement and test `backfill` against fakes, but the live run is
always yours: read `backfill-plan.md`, then run `backfill --yes` by hand.
**Answer:** _(unanswered)_

### `[r5]` 2026-09-05 — Bot account credentials — status: open
The bot needs a roadmap account (`python3 bin/roadmap-users.py add nwnbot --role bot`, after
`r1` lands) and its password in `.env` as `ROADMAP_PASSWORD`.
**Proposed:** you create the account and put the password in `.env`; autopilot never handles
credentials and never commits `.env`.
**Answer:** _(unanswered)_

### `[r6]` 2026-09-05 — Duplicate-match thresholds, and how aggressive to be — status: open
`[b9-dupes]` needs two numbers and one policy call. A false merge steals merit credit; a missed
duplicate just means you merge it by hand in the editor, as you do today.
**Proposed:** start deliberately shy — low threshold 0.55 (mention only), high threshold 0.85
(auto `dupe_of`), and run the first two weeks with the high band **disabled** so every candidate
is only a suggestion in the thread plus a review entry. Turn auto-merge on once the suggestions
have been right consistently. Alternative: never auto-merge at all and always leave it to you.
**Answer:** _(unanswered)_

### `[r7]` 2026-09-05 — Packaging and async-test conventions — status: open
Raised by `[b1-scaffold]`; **blocks nothing** — b7 has a working default either way, so
autopilot proceeds unless you say otherwise. Two conventions the later items inherit:
1. `pyproject.toml` is pytest-config only, so `nwnbot` imports by rootdir happenstance rather
   than being installed. `[b7-cli-runtime]`'s `python -m nwnbot plan` and the systemd unit both
   want an answer.
   **Proposed:** keep it minimal; b7 adds `nwnbot/__main__.py` and sets `WorkingDirectory=` +
   `PYTHONPATH=` in the unit rather than requiring `pip install -e .` on the server.
2. `asyncio_mode = "strict"` — every async test needs an explicit `@pytest.mark.asyncio`.
   **Proposed:** keep `strict`. It costs b3/b6 one decorator per async test and is worth the
   explicitness; changing it later means touching every async test.
**Answer:** _(unanswered)_

### `[r8]` 2026-09-05 — Two player-visible strings from `[b4-render]` — status: open
Blocks `[b10-wording]` only; `[b4-render]` itself is shipped with placeholders marked
`PROVISIONAL WORDING` in the code. Nothing can reach a player until you run `apply --yes`.
1. **Truncation marker.** Discord-bound text cuts at 4000 chars (settled). The marker is not.
   **Proposed:** keep it bare — `…`, a blank line, then the editor URL, no prose. Alternative:
   `… (truncated — full item: <url>)`.
2. **`CDN_EXPIRY_NOTE`.** Currently *"Note: Discord attachment links above are signed and
   expire, so they may 404 later. The attachment was not rehosted."* It lands in the internal,
   never-rendered `comments` list, so the blast radius is small.
   **Proposed:** approve as-is.
3. **Should `code`/`pre` join the roadmap sanitizer whitelist?** Neither is in
   `roadmap_sanitize.ALLOWED_TAGS`, so `md_to_html` deliberately leaves inline code as literal
   backticks — emitting a `<code>` tag would be unwrapped on save and break the fixed point.
   Changing it is a `nwn_homers_lotr` edit (`bin/roadmap_sanitize.py` plus the JS mirror at
   `roadmap-editor.py:4995`).
   **Proposed:** leave it out; backticks read fine in a bug report. Fold into
   `[b2-roadmap-schema]`/`[r1]` only if you want it.
**Answer:** _(unanswered)_

---

## Log

One line per completed item: id · date · commit · what shipped.

`[b1-scaffold]` · 2026-09-05 · b95bfff · First commit: `nwnbot/` package stubs, `tests/` smoke suite, `scraper.py` retired, requirements + `.env.example` extended.
`[b4-render]` · 2026-09-05 · 098c136 · `md_to_html`/`html_to_md` matching the editor's contenteditable shape, stdlib only; fixed point property-tested both directions over 17 real pasted-Discord blobs.
