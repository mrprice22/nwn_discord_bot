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

### `[b9-dupes]` — status: blocked
*Blocked: the two thresholds and the auto-merge policy are unanswered — see review item `r6`.
The seam is already in place: `plan_discord_to_roadmap()` has the hook immediately before the
create-idea branch, `_plan_new_idea` takes `dupe_of`, and `resolve_canonical()` (transitive,
cycle ⇒ review) and `store.DECISION_DUPE_REMOVED` shipped with `[b6-sync]`. This item is a
scoring function and its thresholds, not a rewrite.*
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
Replace every `PROVISIONAL WORDING` placeholder with the answers to `[r8]` and `[r11]`:
in `nwnbot/render.py` the truncation marker (a bare `…` plus the editor URL) and
`CDN_EXPIRY_NOTE`; in `nwnbot/sync.py` the four Discord-bound strings `STATUS_MESSAGE`,
`MERIT_MESSAGE`, `UNLIKELY_MESSAGE`, `THREAD_BODY`, the internal `COMMENT_TEMPLATE`, the
`LINK_SUFFIX` and the `_status_label()` map; in `nwnbot/cli.py` the operator-facing
`APPLY_REFUSAL` and `BACKFILL_REFUSAL`, and in `nwnbot/bot.py` the
`EVENT_DEBOUNCE_SECONDS` threshold (`[r12]`). All are single constants; the surrounding logic
and its tests are shipped and settled.
**Acceptance:** no `PROVISIONAL WORDING` comment remains in `nwnbot/`, the strings match the
answers verbatim, and `pytest` still passes.

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

### `[r4]` 2026-09-05 — Backfill approval gate — status: open
`[b8-backfill]` would create ~100+ forum threads in one run.
**Proposed:** autopilot may implement and test `backfill` against fakes, but the live run is
always yours: read `backfill-plan.md`, then run `backfill --yes` by hand.
**Answer:** _(unanswered)_

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

### `[r11]` 2026-09-05 — Wording and three policy calls from `[b6-sync]` — status: open
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
**Answer:** _(unanswered)_

### `[r12]` 2026-09-05 — Four small calls from `[b7-cli-runtime]` — status: open
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
**Answer:** _(unanswered)_

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
