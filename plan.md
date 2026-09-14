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
- **Login is `POST /api/login`** (`roadmap-editor.py:2976`, public routes at `:1976`), body
  `{"username","password"}` → `Set-Cookie: roadmap_session`. `/login` is the HTML page, not the
  API. `401` on bad credentials, `429` when throttled, `503 {"setup": true}` when no accounts
  exist.
- **A save conflict is HTTP 200, not 409:** `{"ok": false, "conflict": true, "version": …}`,
  with an `overlap: [ids]` key only for a genuine same-idea collision (`:3157` and `:3170`).
  A validation failure is a third 200 shape, `{"ok": false, "errors": [...]}`, with no
  `conflict` key. Only real statuses are 401/403 (route gate) and 503 (lock timeout).
- **Writing `discord:` does not depend on `[b2-roadmap-schema]`.** `gen-roadmap.py:308` only
  *warns* on an unrecognised idea key and `write_document` round-trips it; b2 merely silences
  the warning. Unknown-field warnings come back in `warnings[]` on a successful save.
- `_set_cookie` (`:2337`) marks the session cookie `Secure` unless
  `ROADMAP_AUTH_INSECURE_COOKIE=1`. A throwaway editor on plain `http://127.0.0.1:8799` must
  set that env var or an `aiohttp` cookie jar silently drops the session.
- `MAX_COMMENT_LEN = 3000` (`:2049`). `/api/save` needs the `edit` permission,
  `/api/idea-comment` needs `uat` (`:2007`, `:2013`).
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
  `.venv/bin/python -m pytest -q` (Windows: `.venv\Scripts\python -m pytest -q`), not
  bare `python -m pytest`.
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

### `[b2-roadmap-schema]` — status: done
*Approved and shipped 2026-09-05 as `nwn_homers_lotr` commit `806bd444435`, standalone and
revertible on its own. **Committed locally, not pushed** — merging and restarting the editor
are the admin's.*
In `nwn_homers_lotr`, one standalone commit: add `"discord"` to `IDEA_FIELDS`
(`bin/gen-roadmap.py:160`) and `FIELD_ORDER` (`bin/roadmap-editor.py:77`, after `commit`); add
`"bot": {"view", "edit", "uat"}` to `ROLES` (`bin/roadmap_auth.py:84`) plus a `ROLE_LABELS`
entry and a `BOT_FORBIDDEN` assertion in `bin/roadmap-auth-selftest.py` mirroring
`TESTER_FORBIDDEN`.
**Acceptance:** `python3 bin/roadmap-lint.py` clean, `bin/roadmap-auth-selftest.py` passes, the
editor starts without the import-time drift warning.

### `[b3-roadmap-client]` — status: done
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

### `[b5-config]` — status: done
*Unblocked 2026-09-05: `[r2]` and `[r3]` are answered.*
`nwnbot/config.py`: env loading plus the literal tag-name → group-id dict, validated at startup
against `vocab` from `/api/data` and against the forum's `available_tags`; fail loudly when
either side has drifted. Forum → type: bugs ⇒ `Defect`, feature requests ⇒ `Enhancement`.

**The mapping is settled (`[r2]`) and already committed as `tag-map.json`** — fold those exact
12 pairs into `config.py` as the literal dict, keeping `--tag-map` as the override. Both forums
carry the same 12 tags.

**There is no `Exploit` tag (`[r3]`).** The bot never writes `type: Exploit`; it is an admin
promotion in the editor. Preserve the property that `type` is written only at creation
(`sync.py:842`) so a promotion is never reverted — assert it if you can.

**Still the admin's to do, and not code:** put the two forum channel ids in `.env` as
`DISCORD_BUGS_FORUM_ID` / `DISCORD_FEATURES_FORUM_ID`. `doctor`'s env check fails until they
are set, which is the intended gate — this item does not need to see them.
Player identity map `players.json` (`discord_user_id` → roadmap player name), seeded by parsing
the parentheticals already in `players:` (`"Sync (Shync)"`, `"Balendin (Balendin_2222)"`);
every unmatched author is queued for review, never auto-added.
Also add `DISCORD_BOT_USER_ID` to `.env.example`: `[b7-cli-runtime]` reads it as
`PlanContext.bot_user_id` (loop-prevention layer one) on the live path, and left `.env.example`
alone because this item owns the config surface.
**Acceptance:** `doctor` reports all 12 tags mapped to all 12 groups, and exits non-zero on a
deliberately broken mapping. (`doctor`'s broken-mapping checks — unknown group id, uncovered
group, two tags claiming one group, wrong count, a tag absent from the forum — already ship in
`[b7-cli-runtime]`; this item supplies the real mapping they run against.)

### `[b6-sync]` — status: done
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

### `[b7-cli-runtime]` — status: done
`nwnbot/cli.py`: `doctor`, `plan`, `apply`, `backfill`, `serve`. `apply` refuses without `--yes`
*and* `NWNBOT_DRY_RUN=0`. `nwnbot/bot.py`: `discord.Client` with `message_content` + `guilds`
intents handling `on_thread_create`, `on_message`, `on_raw_thread_update`, plus a 15-minute full
reconcile so the event path and the poll path cannot diverge (both funnel through the same
planner). Ship `systemd/nwnbot.service` as a user unit, `Restart=on-failure`,
`EnvironmentFile=`, deliberately **not** enabled by default — arming it is a decision, with a
header comment saying so (mirror `nwn_homers_lotr/systemd/llm-autopilot.service`).
**Acceptance:** `python -m nwnbot plan` against fakes prints an action list and writes nothing;
`apply` without `--yes` exits non-zero; the unit file passes `systemd-analyze verify`.

### `[b9-dupes]` — status: done
*Unblocked 2026-09-05: `[r6]` is answered, and answered as a third shape rather than either
option it offered — see the entry.*
Duplicate detection for ideas raised more than once in Discord. Runs inside
`plan_discord_to_roadmap()` before the create-idea branch. **The bot never writes `dupe_of`**:
every match is a proposal, and a DM or admin confirms it by setting `dupe_of` in the editor.

- **Score** (`nwnbot/dupes.py`, stdlib only): `difflib.SequenceMatcher` on normalized titles
  blended with token-set overlap over title + first post, group and tag words stripped from both
  sides. `notes` is flattened through the real `html_to_md` and truncated; `impl_notes` is not
  read. O(n) over ~404 candidates, prepared once per run.
- **Three bands**, both thresholds in `config.py`: below low ⇒ silence; low–high ⇒ a review
  entry only; at/above high ⇒ that entry plus a line in the thread, gated by
  `DUPE_POST_IN_THREAD`. **In every band the idea is created normally with no `dupe_of`**, so a
  false positive can never swallow a real report.
- **Confirmation** is observed, not commanded: when a human sets `dupe_of`,
  `plan_roadmap_to_discord` posts once in the reporter's thread, appends a `comment` on the
  canonical naming the second reporter, and lets the existing close path follow the canonical's
  `merit_awarded`. The thread is never archived or locked for being a duplicate.
- **Rejection** is resolving the review entry — no new mechanism, because `Store.view()` already
  loads reviews of every status, so a resolved entry is never re-raised. `review` (new
  subcommand) is what makes that reachable.
- **Un-linking** a `dupe_of` the bot announced files `REVIEW_DUPE_UNLINKED`. The bot does not
  retract on its own.

**The thresholds were measured, not chosen** — `dupes --calibrate` (new subcommand) against the
real `roadmap.yaml`, using its five existing `dupe_of` rows as ground truth. The result is the
item's most important output and is written up in `future-llm-dupe-matching.md`: a token scorer
gets ~20% recall at a 10% false-positive rate on this corpus, and `[r6]`'s proposed 0.55/0.85
would have found **none** of the five. Hence `DUPE_POST_IN_THREAD = False`.

**Acceptance:** met, with one criterion revised on evidence. Table-driven tests cover every band,
the confirmed-dupe path and the un-link path; the "no planner ever writes `dupe_of`" invariant is
asserted directly across all three context shapes; a resolved review is not re-raised; replaying
a plan is still a no-op. The original criterion "the known duplicate clusters score above the
high threshold" is **not achievable and is now asserted as a known limit instead**
(`tests/test_dupes.py`) — the real duplicates in this roadmap are paraphrases, and lexical
overlap cannot see them. `roadmap-lint.py` is unaffected: nothing writes `dupe_of`.

### `[b10-wording]` — status: done
Replace every `PROVISIONAL WORDING` placeholder with the answers to `[r8]` and `[r11]`:
in `nwnbot/render.py` the truncation marker (`… (truncated — full item: <url>)`, the
alternative rather than the bare `…` the entry proposed) and `CDN_EXPIRY_NOTE`; in
`nwnbot/sync.py` the four Discord-bound strings `STATUS_MESSAGE`, `MERIT_MESSAGE`,
`UNLIKELY_MESSAGE`, `THREAD_BODY` — plus the new `THREAD_HEADER` — the internal `COMMENT_TEMPLATE`, the
`LINK_SUFFIX` and the `_status_label()` map; in `nwnbot/cli.py` the operator-facing
`APPLY_REFUSAL` and `BACKFILL_REFUSAL`, and in `nwnbot/bot.py` the
`EVENT_DEBOUNCE_SECONDS` threshold (`[r12]`). All are single constants; the surrounding logic
and its tests are shipped and settled.
**Acceptance:** no `PROVISIONAL WORDING` comment remains in `nwnbot/`, the strings match the
answers verbatim, and `pytest` still passes.

### `[b11-code-tags]` — status: done
*Raised by `[r8]`.3, where the admin overrode the proposal: `<code>`/`<pre>` join the roadmap
sanitizer whitelist rather than being left out. Carved out of `[b10-wording]` because it edits
`nwn_homers_lotr`, and kept standalone and revertible like `[b2-roadmap-schema]`.*
Two commits, one per repo. In `nwn_homers_lotr`: `code`/`pre` into
`roadmap_sanitize.ALLOWED_TAGS`, `pre` alone into `BLOCK_TAGS` (it closes an open `<p>` the way
a browser does; `code` is inline and must not), `CODE`/`PRE` into the editor's `PASTE_TAGS` JS
mirror, and `white-space: pre-wrap` in both the generated page and the contenteditable pane —
a default `<pre>` does not wrap and would push a card wider than the content column. In
`nwn_discord_bot`: `md_to_html` emits both, `html_to_md` reads them back.
**Acceptance:** `roadmap-lint.py` clean; `md_to_html` output comes back from the real
`sanitize_notes` byte for byte; the second round trip is a fixed point with fences in the
corpus.

### `[b8-backfill]` — status: done
*Unblocked 2026-09-05: `[r4]` answered, and the eligibility policy settled with it.*
`backfill` runs `plan_roadmap_to_discord()`, writes `backfill-plan.md` grouped by tag with a
total and the exact command to run it, and stops. Armed, it executes paced with backoff.

**Eligibility is planner policy, not a flag on the command** — the finding that shaped the item.
`backfill` and the live `serve` loop run the *same* planner, so a filter living only in the
command would let `serve` plan a thread for every open item on its first cycle, blow the action
cap and abort — and since `SyncEngine.cycle` aborts whole, that would take the Discord→roadmap
direction down with it. The policy therefore lives in `PlanContext.earns_thread`, is owned by
`config.py`, and both paths share it:
- a **player's** item earns a thread at any open status — someone is waiting to hear back;
- a **staff** item earns one only once it is `soon` or beyond (`STAFF_THREAD_STATUSES`, asserted
  equal to the ordered prefix of `STATUSES` so an inserted status cannot silently move it);
- an item with **no `player`** counts as staff — an unattributed item is the admin's own;
- ineligible is **silent**, not a review item: it is the normal state of most of the backlog,
  and it changes by itself the moment the item is promoted.
- `earns_thread` is consulted **only on the create branch**, so demoting an item never orphans a
  thread it already has.

Arming needs three things, not two: `--yes`, `NWNBOT_DRY_RUN=0`, **and `--cap` at least the
planned count**. The count is the confirmation — `[r4]` settled that the live run is the
admin's, and typing the number is how they take it. The report is written even when the cap
refuses, because it is what you read before confirming.

`[b7]` already checkpointed each new thread into both the store and the idea's `discord:` field
immediately, so the resume story needed no new code — only the pacing
(`BACKFILL_MIN_INTERVAL`, 2s), the 429 backoff (`BACKFILL_MAX_RETRIES`, doubling from the
server's own `retry_after`), and letting `--yes` through.

**Acceptance:** met. A writer that dies after two threads leaves two checkpointed on both sides;
the resume opens exactly the remaining four and no idea ends with two threads. Verified beyond
the fixtures against a snapshot of the **live** roadmap: 97 threads planned, 97 created through
fakes with 97 `discord:` writes and 0 failures, and an immediate re-run planned **0**.

---

## Needs human review

Autopilot appends here and sets the blocking item to `blocked`. Never decide one of these
yourself. Each entry is dated, states the question, and proposes an answer so it can be
approved or adjusted in one line.

### `[r1]` 2026-09-05 — Approve the `nwn_homers_lotr` schema change? — status: answered
`[b2-roadmap-schema]` edits the roadmap editor, a service you use daily. It adds a `discord`
field to `IDEA_FIELDS` + `FIELD_ORDER` and a `bot` role to `ROLES`.
**Proposed:** approve as a single standalone commit, reviewed and merged before any bot code
lands, so it can be reverted independently. The `bot` role is `{view, edit, uat}` — no
`promote_shipped`, `merit`, `publish`, `audit_view` or `merit_view`, so the bot cannot ship an
item or pay merit even if it tries.
**Answer:** 2026-09-05 — approved, and shipped as `nwn_homers_lotr` commit `806bd444435`
(committed locally on `main`, **not pushed**; merging and restarting the editor are the
admin's). The role is `{view, edit, uat}` and denies the other 11 capabilities.

The case turned out stronger than this entry assumed: `enforce_idea_permissions()`
(`roadmap_auth.py:743`) *independently* refuses, for any caller lacking the capability, to
create or move an item into a shipped status, to change `merit_awarded` in either direction, to
change a paid UAT credit, or to delete a shipped or merit-paid item. That is the same list
`[b3]`'s client refuses to *send*, so the guard is now two independent layers and the
server-side one is beyond the bot's reach — which is what `[r9]` was worried about.

Two details confirmed by reading the code rather than assuming: `edit` alone suffices to create
ideas (`submit` is unused by `/api/save` and reserved for a future `player` role), and `uat` is
what `/api/idea-comment` gates on. So `{view, edit, uat}` is exactly sufficient and no more.
Beyond the entry's description, `BOT_FORBIDDEN` also got the *whitelist* equality check the
tester tier has (`bot.caps == {"view","edit","uat"}`) plus a partition assertion that every
`CAPS` entry is either granted or explicitly denied — that is the half that catches a future
capability nobody remembered to forbid.
**Verified:** `roadmap-lint.py` clean (409 ideas), `roadmap-auth-selftest.py` all checks passed,
`FIELD_ORDER ^ IDEA_FIELDS` empty (checked by parsing both files, not by importing the editor,
so the running service was never touched).

### `[r2]` 2026-09-05 — The 12 forum tag names and the two forum channel ids — status: answered
Needed for `[b5-config]`; not discoverable from either repo.
**Proposed:** paste the output of a `doctor --dump-tags` run (or the tag list from the forum
settings) and the two channel ids into `.env` / `config.py`. If a tag name matches a group
`title` closely enough the mapping can be seeded automatically, but it must be confirmed by
hand once. (Note: `doctor --dump-tags` was never built — `[b7]` shipped `doctor` with
`--fixture`, `--db`, `--tag-map`, `--cap`, `--check-roadmap` only.)
**Answer:** 2026-09-05, admin supplied the 12 tag names directly. They are committed as
`tag-map.json` and validate one-to-one onto the 12 groups
(`doctor --tag-map tag-map.json` → `12 tag(s) mapped one-to-one onto all 12 groups`):

| forum tag | group id |
|---|---|
| `Forge & Crafting` | `forge` |
| `Combat & Classes` | `combat-classes` |
| `Bosses & Difficulty` | `bosses` |
| `Bestiary/Achievement` | `progression` |
| `Teleports & Travel` | `travel` |
| `Banking & Storage` | `banking` |
| `Wiki & Tools` | `wiki-tools` |
| `Quests & Areas` | `quests-areas` |
| `Items & Gear` | `items-gear` |
| `Companions/Henchmen` | `meaningwave` |
| `Economy & Merit` | `economy` |
| `QualityOfLife/Buffs` | `qol` |

Eleven are near-verbatim matches to the group `title`. `Companions/Henchmen` → `meaningwave`
("Meaningwave Companions") was matched by elimination and is the one line worth a second look.
The two **channel ids were deliberately not pasted into the repo or the conversation** — they
are the admin's to put in `.env` as `DISCORD_BUGS_FORUM_ID` / `DISCORD_FEATURES_FORUM_ID`, and
`doctor`'s env check already fails loudly until they are set.

### `[r3]` 2026-09-05 — Where do `Exploit` items come from? — status: answered
`type: Exploit` is worth 3 merit but neither forum implies it: bugs ⇒ `Defect`, feature requests
⇒ `Enhancement`.
**Proposed:** add an `Exploit` tag to the bugs forum and let it override the type. Alternative:
leave `Exploit` as an admin-only reclassification in the editor, and have the bot never set it.
**Answer:** 2026-09-05 — the alternative. *"Exploits are either reported in the bugs channel or
messaged to admin directly."* So there is **no `Exploit` tag**, both forums carry the same 12
tags, and the bot only ever writes `type: Defect` (bugs) or `type: Enhancement` (feature
requests). An exploit reported in `#bugs` is created as a `Defect` and the admin promotes it to
`Exploit` in the editor; one sent by DM never reaches the bot at all.
**Verified this is safe:** the planner writes `type` only at creation (`nwnbot/sync.py:842`) and
the only field it ever updates on an existing idea is `group`, so a manual promotion to
`Exploit` is never reverted by a later sync. Any future item that wants to update `type` must
keep that property or it silently downgrades an exploit from 3 merit to 1.

### `[r4]` 2026-09-05 — Backfill approval gate — status: answered
`[b8-backfill]` would create ~100+ forum threads in one run.
**Proposed:** autopilot may implement and test `backfill` against fakes, but the live run is
always yours: read `backfill-plan.md`, then run `backfill --yes` by hand.
**Answer:** 2026-09-05 — approved as proposed, and the admin settled the eligibility policy at
the same time, which turned out to be the larger half of the question.

The item as written would have opened a thread for every open item: **189** of them, of which
**125 are the admin's own** and 113 are `planned`/`later` — threads addressed to nobody, sitting
dead in the forum. Counting first is what turned a one-line approval into a policy:

> All open items that are **reported by a player** (any open status), **or** reported by an
> admin/DM **and** at `soon`, in progress, or beyond — so `planned`/`later` are ignored for
> staff-reported ideas.

Measured against the live roadmap: **97 threads**, 61 player-reported and 36 staff near-term.
(99 qualify; two are skipped because they carry **no `type`** — `resize-dragonshape-so-it-can-fit-through-doors-transitions`
and `ring-bearer-quest` — and the planner refuses to guess which forum or how much merit. Those
are two real gaps in `roadmap.yaml` worth filling.)

The live run stays the admin's, now behind three keys rather than two: `--yes`,
`NWNBOT_DRY_RUN=0`, and a `--cap` naming the planned count.

### `[r5]` 2026-09-05 — Bot account credentials — status: answered
The bot needs a roadmap account (`python3 bin/roadmap-users.py add nwnbot --role bot`, after
`r1` lands) and its password in `.env` as `ROADMAP_PASSWORD`.
**Proposed:** you create the account and put the password in `.env`; autopilot never handles
credentials and never commits `.env`.
**Answer:** 2026-09-05 — the admin overrode the proposal and asked for the account and password
to be generated directly. Done: account `nwnbot`, role `bot`, display name "Sync Bot", created
in the live auth DB via `roadmap-users.py add --stdin` so the password never entered `argv`,
`ps`, or a shell history. The password is 256 bits from `secrets.token_urlsafe(32)`, was never
printed, and lives only in `.env` (now mode 600). Verified absent from every tracked file in
both repos and from the whole of git history. `ROADMAP_BASE_URL` and `ROADMAP_USER` were set at
the same time.
**Rotation:** `python3 bin/roadmap-users.py passwd nwnbot` (it revokes the account's sessions);
update `ROADMAP_PASSWORD` in `.env` to match.

### `[r6]` 2026-09-05 — Duplicate-match thresholds, and how aggressive to be — status: answered
`[b9-dupes]` needs two numbers and one policy call. A false merge steals merit credit; a missed
duplicate just means you merge it by hand in the editor, as you do today.
**Proposed:** start deliberately shy — low threshold 0.55 (mention only), high threshold 0.85
(auto `dupe_of`), and run the first two weeks with the high band **disabled** so every candidate
is only a suggestion in the thread plus a review entry. Turn auto-merge on once the suggestions
have been right consistently. Alternative: never auto-merge at all and always leave it to you.
**Answer:** 2026-09-05 — neither, in three parts.

**(1) Auto-merge is removed, not deferred.** The bot never writes `dupe_of` in any band. A
duplicate becomes real only when a DM or admin sets `dupe_of` in the editor — the action the
admin already takes today — and the bot's job is to notice and tidy up after it: tell the
reporter, note the extra demand on the canonical, and close on the canonical's `merit_awarded`.
Asserted directly across every planner path, not left as a comment.

**(2) The bands are quiet vs. loud, not suggest vs. merge.** Below low, silence; low–high, a
review-queue entry and nothing said in Discord; above high, that entry plus one line in the
thread. In every band the reporter's own idea is created normally, so a false positive can never
swallow a real report. Rejecting a suggestion is resolving its review entry — which needed no
new mechanism, because `Store.view()` already loads reviews of every status, but did need the
new `review` subcommand to be reachable at all.

**(3) The numbers are measured, and the measurement changed the answer.** `dupes --calibrate`
was run against the real `roadmap.yaml`, using its **five existing `dupe_of` rows as ground
truth**. Findings, in full in `future-llm-dupe-matching.md`:

- The five known duplicates score 0.10–0.51. Only **2 of 5** rank their true canonical first;
  the rest land at #4, #19 and #93.
- Scoring all 404 ideas as fresh threads, the top-1 match — a false positive by construction —
  is ≥0.20 for 53% of them, ≥0.30 for 18%, ≥0.50 for 10%.
- **The proposed 0.55/0.85 would have found none of the five.**
- The strongest lexical signals in the corpus are *deliberately distinct* siblings: "Prestige
  quest: Pale Master (L11+)" vs "Prestige quest: Weapon Master (L13+)" scores 0.83, above every
  real duplicate.

The real duplicates here are **paraphrases** ("rest-menu teleport back to where you last ported"
vs "expand rest-menu teleports"), which lexical overlap cannot see. So the shipped values are
`low=0.50`, `high=0.85`, `title_weight=0.3`, and **`DUPE_POST_IN_THREAD = False`**: a matcher
right about one duplicate in five has not earned a player-visible claim. The admin still sees
every candidate in the review queue. The gate is one line to flip.

**This is the evidence for the LLM path**, and it arrived before shipping rather than after
months of production. Recovering the other 80% needs semantic matching — designed, costed and
measured in `future-llm-dupe-matching.md`, and not built.

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

### `[r8]` 2026-09-05 — Two player-visible strings from `[b4-render]` — status: answered
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
**Answer:** 2026-09-05 — (1) the **alternative**, not the proposal: the marker says the text was
*cut*, `… (truncated — full item: <url>)`. A bare `…` reads like the report simply trailed off.
With no editor URL there is nothing to say, so the bare `TRUNCATION_MARKER` stays as the
fallback and is asserted both ways. (2) `CDN_EXPIRY_NOTE` approved as-is.
(3) **The admin overrode the proposal: `code`/`pre` go in.** That is a `nwn_homers_lotr` edit,
so rather than reopen `[b2]` — already committed there — it became its own item,
`[b11-code-tags]`, shipped as `dd91ee5a4a3` (local, **not pushed**; merging and restarting the
editor are the admin's, as with `[b2]`).
**Verified:** `roadmap-lint.py` clean (409 ideas), `tests/check_roadmap_notes.py` passes
(7 fixtures + 518 notes), and — the check that actually matters across the two repos —
`md_to_html` output comes back from the real `sanitize_notes` byte for byte on six
representative blobs, with `md -> html -> md` a fixed point through it.
**One real risk, handled:** a default-styled `<pre>` does not wrap, so without the added
`white-space: pre-wrap` a long log line would have pushed its roadmap card wider than the
content column and taken the grid with it.

### `[r9]` 2026-09-05 — Which roadmap account does the bot use before `[r1]` lands? — status: answered
Raised by `[b3-roadmap-client]`, and the sharpest of the three questions it produced. There is
no `bot` role in `roadmap_auth.py:84` today, so `ROADMAP_USER` would have to be an existing
`admin` or `dm` account — i.e. one holding `promote_shipped` and `merit`. That makes the
client's own assertions the *only* thing standing between the bot and shipping an item or
paying merit, rather than the second line of defence they are designed to be.
**Proposed:** do not point the bot at any live roadmap account until `[r1]` is answered and the
`bot` role exists. Ties directly to `[r5]`. Nothing in the code needs to change either way —
this is about what goes in `.env`.
**Answer:** 2026-09-05 — resolved as proposed, in the strong form: no live account was ever
configured before `[r1]` landed. `.env` now holds the `nwnbot` account, which *is* the `bot`
role, so the client's assertions are the second layer rather than the only one. The concern
this entry raised no longer applies.

### `[r10]` 2026-09-05 — Two smaller calls from `[b3-roadmap-client]` — status: open
Blocks nothing; both are implemented with the conservative option and are cheap to reverse.
1. **A no-delete assertion that is not in the item text.** `/api/save` posts the whole document,
   so an existing idea id missing from the array is a *delete*. The client raises
   `ForbiddenWrite` on that. It can only ever prevent a write, never cause one, and neither
   `[b6-sync]` nor `[b9-dupes]` deletes.
   **Proposed:** keep it; if some future item genuinely needs to delete an idea, it gets an
   explicit opt-in argument rather than the rule being dropped.
2. **Where a `SaveConflict` goes.** The client surfaces it and stops, as specified — it never
   forces. Where "queue for review" actually lands is a `[b6]`/`[b7]` decision.
   **Proposed:** `[b7-cli-runtime]`'s `apply` catches it, records it in the store, prints it in
   the run summary and exits non-zero. No retry, no forcing.
**Answer:** _(unanswered)_

### `[r11]` 2026-09-05 — Wording and three policy calls from `[b6-sync]` — status: answered
Blocks `[b10-wording]`. Nothing here can reach a player until you run `apply --yes`; all four
are implemented with the most conservative option and marked `PROVISIONAL` in the code.
1. **The player-visible strings.** `STATUS_MESSAGE`, `MERIT_MESSAGE`, `UNLIKELY_MESSAGE`,
   `THREAD_BODY`, plus the internal-only `COMMENT_TEMPLATE`, `LINK_SUFFIX` and the terse
   `_status_label()` map over the ten statuses.
   **Proposed:** review them as a second batch alongside `[r8]` — approve the placeholders or
   replace them verbatim.
2. **Where a bot-created idea starts.** Implemented as `status: planned` — the only one of the
   ten that describes a report nobody has triaged, and not one of the admin-only three.
   **Proposed:** approve `planned`. The alternative is a new triage status, which is a schema
   change and would mean reopening `[r1]`.
3. **Should the bot announce current status the first time it adopts a thread it did not
   open?** Implemented as silent adoption: a `RecordBaseline` action takes the thread on and
   only *later* changes are posted, so nothing reaches a player retroactively.
   **Proposed:** keep quiet adoption — the alternative posts a status line into every existing
   thread the first time the bot runs.
4. **Should an archived/locked thread with no roadmap idea be backfilled into one?** Currently
   skipped silently.
   **Proposed:** keep skipping. Old closed threads are history, and a bulk import is your call,
   not a side effect of the bot's first run.
**Answer:** 2026-09-05 — (2), (3) and (4) approved as proposed: `NEW_IDEA_STATUS = "planned"`,
quiet adoption, archived threads with no idea stay skipped. (1) the strings were **tightened**
rather than approved verbatim: `STATUS_MESSAGE` leads with the plain-language label and trails
the raw status id (kept, because it is the word the editor and the roadmap page use, so a
player who goes looking finds the same term); `MERIT_MESSAGE` gained a comma;
`UNLIKELY_MESSAGE` now says the archive is *unlocked*, which is easy to miss.
`COMMENT_TEMPLATE`, `LINK_SUFFIX` and `_status_label()` are unchanged.

**A fifth call, raised and answered here:** `THREAD_BODY` said nothing about where a bot-opened
thread came from. `[b8-backfill]` will open one per open item, so without it a player meets a
bot posting their own words back at them with no explanation. `THREAD_HEADER` now leads the
opening post — so it survives the 4000-char cut by construction — and `_plan_new_thread` joins
it to the body rather than formatting it in, so an item with empty `notes` gets the header
alone and not a leading blank. Both are asserted.

### `[r12]` 2026-09-05 — Four small calls from `[b7-cli-runtime]` — status: answered
Blocks `[b10-wording]` only. All four are implemented with the conservative option and marked
`PROVISIONAL` in the code; none can reach a player.
1. **`EVENT_DEBOUNCE_SECONDS = 5.0`** (`nwnbot/bot.py`) — how long the worker lets a burst of
   forum activity settle before running one cycle. A threshold plan.md never stated.
   **Proposed:** approve 5 s. A five-message conversation then costs one reconcile instead of
   five, and the 15-minute reconcile is the backstop if a burst is ever missed.
2. **`APPLY_REFUSAL` and `BACKFILL_REFUSAL`** (`nwnbot/cli.py`) — terminal-only wording, no
   blast radius. **Proposed:** approve as written, or fold into the `[r8]`/`[r11]` batch.
3. **The unit's live switch stays split.** `systemd/nwnbot.service` ships un-enabled and
   deliberately does *not* set `NWNBOT_DRY_RUN`, so `systemctl --user restart` can never
   quietly arm the bot — going live stays a separate edit to the environment file.
   **Proposed:** confirm that split, and confirm
   `WorkingDirectory=/var/home/james/GIT/nwn_discord_bot` is the right path on the machine that
   will actually run it.
4. **The interim home for the tag mapping.** `serve` and a live `plan` currently require a
   `--tag-map` JSON file and exit naming `[b5-config]`/`[r2]` rather than guessing tag names.
   **Proposed:** confirm a JSON file is the right interim home, or say the mapping should wait
   entirely for `[b5]`.
**Answer:** 2026-09-05 — (1) and (2) approved as proposed: `EVENT_DEBOUNCE_SECONDS = 5.0`,
`APPLY_REFUSAL` and `BACKFILL_REFUSAL` as written. (3) the split live switch is confirmed, and
`WorkingDirectory=/var/home/james/GIT/nwn_discord_bot` is **verified correct on this machine**:
`/home` is a symlink to `/var/home`, so the `/var/home` form is the real path, which is what
`ProtectSystem=strict` plus `ReadWritePaths=` need to resolve. (4) **moot — self-answered by
`[b5-config]`**: `TAG_GROUPS` is compiled into `nwnbot/config.py` and `cli.tag_map_for` falls
back to it (`cli.py:189`), so `--tag-map` is now an override for a renamed tag, not a
requirement, and there is no interim home to choose.

### `[r13]` 2026-09-05 — Player identity, from `[b5-config]` — status: open
Blocks nothing; the conservative option is implemented. **This one is merit money** — a wrong
match pays the wrong player and nothing detects it afterwards.
1. **May the bot ever resolve a player by display name?** Today: never — only an explicit
   `discord_user_id`. A Discord id appears nowhere in `roadmap.yaml`, so `doctor
   --seed-players` can only write the right-hand column (19 roster names, 8 with a
   parenthetical) and leaves `discord_ids` empty; the aliases are inert, asserted by a test.
   **Proposed:** keep never. If you want a middle ground, the honest one is that an alias match
   becomes a review entry *naming the candidate*, still never an auto-match.
2. **`HomelessSon (Server Admin)`** — that parenthetical is a job title, not a handle, and the
   parser cannot tell the difference. **Proposed:** leave it an ordinary candidate; you will
   resolve your own id by hand in one line.
3. **`doctor --seed-players` makes an otherwise read-only command write once**, under an
   explicit flag, to a gitignored path. **Proposed:** approve; the alternative is a sixth
   subcommand for a one-shot bootstrap.
4. **`NWNBOT_PLAYERS`** is a new env var (default `players.json`), mirroring `NWNBOT_DB`.
   **Proposed:** approve.
5. **`Companions/Henchmen` → `meaningwave`** was matched by elimination in `[r2]` and is now
   compiled into `config.py`. **Proposed:** confirm it before `serve` ever runs — a wrong pair
   files every companion report under the wrong group, silently.
**Answer:** _(unanswered)_

### `[r14]` 2026-09-05 — Three new strings from `[b9-dupes]` — status: open
Blocks nothing; all three ship marked `PROVISIONAL WORDING` and **the first two are unreachable
today**, because `DUPE_POST_IN_THREAD` is off and no duplicate is confirmed automatically.
1. **`DUPE_HINT_MESSAGE`** — posted in a new thread whose report scored above the high band.
   Currently: *"This looks like it may already be tracked as **X** — an admin will check. Either
   way your report is logged and stays yours."* Written as a question, not a verdict.
2. **`DUPE_CONFIRMED_MESSAGE`** — posted after a human confirms. Currently: *"Confirmed as the
   same issue as **X**, which is where it will be tracked from here. This thread stays open and
   your report still counts towards merit."* The second sentence is the point: "duplicate" reads
   like "dismissed" everywhere else, and here it must not.
3. **`DUPE_CANONICAL_COMMENT`** — internal, never rendered: *"Also reported by {player} in
   Discord{where}. Tracked as duplicate {idea_id}."*
**Proposed:** approve as written, in the `[r8]`/`[r11]` style.
**Answer:** _(unanswered)_

### `[r15]` 2026-09-05 — Confirm the measured duplicate settings — status: open
Blocks nothing; the conservative option is shipped. Raised by `[b9-dupes]`, which measured rather
than guessed and got an uncomfortable answer.
`low=0.50`, `high=0.85`, `title_weight=0.3`, `DUPE_POST_IN_THREAD=False`. On this corpus that is
~20% recall at a ~10% false-positive rate; see `[r6]` and `future-llm-dupe-matching.md`.
**Proposed:** confirm the gate stays off, and treat the review-queue entries as the whole feature
for now. Re-run `python -m nwnbot dupes --calibrate --roadmap-yaml <path>` after any change to
the scorer, and before arming `serve`. Turning the gate on is a decision, not a tuning step —
and on this evidence the thing that earns it is semantic matching, not a different number.
**Answer:** _(unanswered)_

### `[r17]` 2026-09-13 — The awarded-exclusion, and what it does not fix — status: answered
Raised and answered in the same session, because it came from the admin as a rule rather than
as a question: *"I try not to reopen ideas once they've been awarded/fully deployed — once
defects or related improvements are reported after that I log new stories as non-duplicate."*

Shipped now: `dupes.is_shipped()` (`merit_awarded: true`, or status `awarded`/`implemented`)
marks an idea as never a merge target. `unlikely` is deliberately NOT shipped — nothing was
delivered, so a second report of it is a genuine duplicate. A match against shipped work is
still *scored*, and filed as `REVIEW_DUPE_ECHO`: a new story in its own right, flagged as a
possible regression in, or follow-up to, that work. Nothing is said to the player on that path
— telling someone who just hit a bug that it was already fixed is the wrong answer.

**Measured on the real corpus, not asserted:** 200 of 412 ideas (49%) leave the merge pool.
Of the admin's 17 recorded verdicts in `nwn_homers_lotr/dupe-suggestions.json`, only 6 describe
a scenario the bot can face — the other 11 are both-sides-shipped pairs, i.e. archival tidying
at which no new report exists. Of those 6 the rule gets 4 right: three `no` verdicts become
echoes, and the one true duplicate is still proposed.

**What it does not fix**, and the two cases are worth naming because they set up the next item:
- `Prestige quest: Harper Scout (L6+)` vs `Prestige quest: Shifter (L16+)` — a *series*, where
  a shared prefix and a differing tail mean sibling, not duplicate. The same shape produces the
  worst false positives in the whole corpus (`Sorcerer line I` vs `Sorcerer line II`, 0.87), and
  a prefix/series rule would catch all of them cheaply.
- `Area authoring: the Grey Havens (+ Cirdan NPC)` vs `Quest: The Last Ship's Cargo (Grey
  Havens)` — same place, different work. Text cannot separate these; this is the case `[r15]`
  means by semantic matching.

### `[r16]` 2026-09-05 — Who counts as staff? — status: answered
Blocks nothing; the conservative option is shipped. Raised by `[b8-backfill]`, whose eligibility
policy turns on it.
Nothing in `roadmap.yaml` marks a role — `players:` is a flat list of 19 names — so
`config.STAFF_PLAYERS` is an explicit list, not something derived. It currently holds one name:
`HomelessSon (Server Admin)`. A name absent from it is treated as a player, which is the
generous direction: the cost of being wrong is one extra thread, never a missed notification.
**Proposed:** confirm that is the whole staff list before `backfill --yes` runs. If any DM has
filed roadmap items under their own name, add them — otherwise their `planned`/`later` items
each open a thread nobody is waiting on. Of the 19 names, the ones with enough open items to
matter are `Sync (Shync)` (14), `Rajmund (Ray)` (11) and `Tukwut` (9); the rest have five or
fewer.
**Answer:** 2026-09-13 — confirmed as proposed. `HomelessSon (Server Admin)` is the
whole staff list; no DM has filed roadmap items under their own name. `config.STAFF_PLAYERS`
already holds exactly that one name, so nothing changed in code — this entry records the
decision rather than a diff. Every other name on the roster is treated as a player, which is
the generous direction: `Sync (Shync)`, `Rajmund (Ray)` and `Tukwut` keep their threads.

---

## Log

One line per completed item: id · date · commit · what shipped.

`[b1-scaffold]` · 2026-09-05 · b95bfff · First commit: `nwnbot/` package stubs, `tests/` smoke suite, `scraper.py` retired, requirements + `.env.example` extended.
`[b4-render]` · 2026-09-05 · 098c136 · `md_to_html`/`html_to_md` matching the editor's contenteditable shape, stdlib only; fixed point property-tested both directions over 17 real pasted-Discord blobs.
`[b3-roadmap-client]` · 2026-09-05 · 40e81ad · Async `RoadmapClient`; forbidden writes enforced as diffs against the server baseline and raised before any request; conflict retried exactly once, never forced; 44 fake-transport tests.
`[b6-sync]` · 2026-09-05 · d73fc87 · Both planners pure (no I/O, clock or randomness); forbidden writes unconstructible; ids mirror the editor's own slugify rules; 129 tests, in-sync plans nothing and replaying a plan is a no-op.
`[b7-cli-runtime]` · 2026-09-05 · c8566a3 · Five subcommands, the debounced event runtime sharing one planner with the 15-minute reconcile (asserted by a test, not a convention), and an un-armed systemd user unit; `apply` needs `--yes` *and* `NWNBOT_DRY_RUN=0`.
`[b5-config]` · 2026-09-05 · 8bbbd2c · The settled 12-tag map folded into `config.py`, one drift rule set shared by `doctor` and the live path, `type` made creation-only in two places, and the silent `DISCORD_BOT_USER_ID` gap closed.
`[b2-roadmap-schema]` · 2026-09-05 · nwn_homers_lotr@806bd444435 · The `discord` idea field and a `{view, edit, uat}` `bot` role, with `BOT_FORBIDDEN` plus whitelist and partition assertions; committed there, not pushed.
`[b10-wording]` · 2026-09-05 · 98b597c · Every `PROVISIONAL WORDING` marker replaced by the settled string and the review item that settled it; the truncation marker now says the text was cut, and a bot-opened thread says where it came from.
`[b11-code-tags]` · 2026-09-05 · 3216a94 + nwn_homers_lotr@dd91ee5a4a3 · `<code>`/`<pre>` on the sanitizer whitelist in all four places it is mirrored, and the render round trip to match; verified against the real `sanitize_notes`, not a local copy of it.
`[b9-dupes]` · 2026-09-05 · a0fba1f · Stdlib duplicate scoring wired into the existing planner seam, every outcome a proposal and `dupe_of` unwritable by any path; thresholds measured against the roadmap's own five confirmed duplicates rather than guessed, which is what put `DUPE_POST_IN_THREAD` off and produced `future-llm-dupe-matching.md`; `review` and `dupes` subcommands added because rejecting a suggestion and calibrating a threshold were both unreachable.
`[b8-backfill]` · 2026-09-05 · 4de51f3 · The batch, paced and resumable — but the item's real content turned out to be *who earns a thread*: eligibility is planner policy shared by `backfill` and `serve`, because a filter in the command alone would have jammed every cycle at the action cap. 189 candidates down to 97, verified end to end against a live-roadmap snapshot.
