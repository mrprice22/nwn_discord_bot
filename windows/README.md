# Running nwnbot on Windows

The bot was written on the Linux box that runs the NWN server and the roadmap editor
(`192.168.1.191`), and `systemd/nwnbot.service` describes that machine. It now *runs* on a
Windows host, reaching the roadmap over the cloudflared tunnel
(`ROADMAP_BASE_URL=https://roadmap.homerslotr.com`) rather than over localhost — so there is no
LAN dependency and the editor host can be down without the bot caring.

This directory is the Windows half of the systemd unit. Two files, and the split between them
is the point:

| File | The systemd thing it replaces |
|---|---|
| `run-nwnbot.ps1` | `EnvironmentFile=`, `WorkingDirectory=`, `Environment=PYTHONPATH=` |
| `nwnbot-task.xml` | the unit itself: `Restart=on-failure`, `Nice=`, `[Install]` |

## Why the wrapper exists

Nothing in `nwnbot/` ever reads `.env` — `config.py:8` says so deliberately, so that a test can
set `NWNBOT_DRY_RUN=0` without going anywhere near real credentials. The environment is always
the caller's job. On Linux `EnvironmentFile=` was that caller. Task Scheduler has no equivalent,
so `run-nwnbot.ps1` parses `.env` into the process environment and does nothing else.

It does **not** set `NWNBOT_DRY_RUN`. Neither does the task XML. Whatever `.env` says is what
the bot does, exactly as on Linux — so re-registering the task, restarting it, or rebooting can
never quietly arm the bot. Going live stays one deliberate edit to `.env`.

## Setup

```powershell
# 1. Python 3.12 (NOT the Microsoft Store build, which is the stub that
#    shadows `python` on a fresh box).
winget install --id Python.Python.3.12 --scope user
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. .env from the Linux host -- it is gitignored and never committed.
scp james@192.168.1.191:~/GIT/nwn_discord_bot/.env .

# 3. Seed the identity map. players.json is NOT on the Linux host; it is
#    generated here, and comes out with discord_ids empty on purpose --
#    no Discord id appears anywhere in roadmap.yaml, so nothing can be
#    guessed. Fill discord_ids in by hand.
powershell -ExecutionPolicy Bypass -File windows\run-nwnbot.ps1 -Command "doctor --check-roadmap --seed-players players.json"

# 4. Read-only checks. Neither writes anything.
powershell -ExecutionPolicy Bypass -File windows\run-nwnbot.ps1 -Command "doctor --check-roadmap"
powershell -ExecutionPolicy Bypass -File windows\run-nwnbot.ps1 -Command "plan"
```

`doctor` should report all 12 forum tags mapped one-to-one onto the 12 roadmap groups, a
successful login as the `nwnbot` account, and every `players.json` entry resolving to a name on
the roster. Expect `plan` to raise `unknown_author` review items for anyone not yet in
`players.json` — that is the identity map being incomplete, not a failure, and by design no name
is ever auto-added to the roadmap's `players:` list.

## Scheduling it (only once the above is green)

Edit `nwnbot-task.xml` first: replace `BOT_RUN_AS_USER` with your account and `BOT_REPO_DIR`
with the full path to this repo. Then:

```powershell
schtasks /Create /TN nwnbot /XML windows\nwnbot-task.xml
schtasks /Change /TN nwnbot /ENABLE     # the arming step; the XML ships disabled
```

With `NWNBOT_DRY_RUN=1` still in `.env` this is safe: the bot plans on every forum event and
every 15 minutes and writes nothing anywhere. Setting it to `0` is the second, separate
decision.

## Differences from the Linux unit worth knowing

- **No sandboxing.** `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` and
  `ReadWritePaths=` have no Task Scheduler equivalent. `RunLevel=LeastPrivilege` is as close as
  it gets — run the task as an ordinary user account, never as an administrator.
- **Paths.** `.venv/bin/python` on Linux is `.venv\Scripts\python.exe` here. `plan.md` and
  `CLAUDE-autopilot.md` were written against the Linux layout.
- **Logging.** systemd sent stdout to the journal. Task Scheduler discards it; if you want a
  log, redirect inside `run-nwnbot.ps1` rather than in the task arguments.

## The local LLM

Duplicate detection asks a local model whether a new report is the same issue as
an existing idea. It runs here, on this box, beside the bot.

| | |
|---|---|
| Server | `llama-server.exe`, installed via `winget install ggml.llamacpp` |
| Model | `D:\models\Qwen3.6-35B-A3B-Q4_K_M.gguf` (19 GB, 35B MoE with 3B active) |
| Endpoint | `http://127.0.0.1:8080/v1` — OpenAI-compatible, loopback only |
| Launcher | `windows\run-llama.ps1` |
| Task | `nwnbot-llm`, at logon, **enabled** |

Unlike `nwnbot`, this task ships enabled and starts itself: the model answers
questions and writes nothing, so there is nothing to arm. It takes ~45s to load.

Two measurements worth keeping, both taken on this hardware against real pairs
from `roadmap.yaml`:

- **~2.0s per duplicate judgement**, which is what makes per-idea judging at
  intake affordable.
- **Qwen3.6 is a reasoning model.** With thinking on, the answer lands in
  `reasoning_content` and `content` comes back **empty** — a naive parser reads
  every verdict as a negative and scores suspiciously well on the negatives. The
  bot sends `chat_template_kwargs: {"enable_thinking": false}`; leave it that
  way, and be suspicious of any agreement number that was measured without it.

The bot never blocks on the model: if it is down, duplicate scoring falls back
to the token scorer and says so in the run summary. `doctor` reports which model
answered.

## Reclaiming the box to game on

Two desktop shortcuts, created by `windows\make-shortcuts.ps1`:

- **Bots - stop (free the GPU)** — stops the LLM server and the bot, waits for
  the memory to actually be released, and **disables** both scheduled tasks so a
  reboot mid-session does not start them again underneath you.
- **Bots - start** — re-enables both, starts them, and waits until the model
  answers before telling you it is ready.

Neither script touches `NWNBOT_DRY_RUN`. That lives in `.env` and is the
live/not-live switch: starting the bot must never be the thing that arms it.
