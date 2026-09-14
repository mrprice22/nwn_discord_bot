# Launch `python -m nwnbot serve` with .env loaded into the environment.
#
# Why this exists: nothing in nwnbot/ ever reads .env (config.py:8 says so
# deliberately -- the environment is the caller's job, so a test can set
# NWNBOT_DRY_RUN=0 without touching real credentials). On Linux the systemd
# unit's `EnvironmentFile=` did that job. Task Scheduler has no equivalent, so
# this wrapper is the Windows half of it and does nothing else.
#
# It deliberately does NOT set NWNBOT_DRY_RUN. Whatever .env says is what the
# bot does, exactly as on Linux: re-running the scheduled task can never
# quietly arm the bot. Going live stays a separate, deliberate edit to .env.
#
# Run by hand to check it before ever scheduling it:
#     powershell -ExecutionPolicy Bypass -File windows\run-nwnbot.ps1 -Command "doctor"

param(
    # The nwnbot subcommand and its flags, as ONE quoted string:
    #     -Command "doctor --check-roadmap"
    # It has to be one string because `powershell -File` hands every argument
    # over as a plain string and treats a leading `-` as a parameter name of
    # its own, so a real argument array never survives the trip.
    [string]$Command = "serve"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo ".env"
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $envFile)) {
    throw "No .env at $envFile. Copy it from the Linux host (~/GIT/nwn_discord_bot/.env); it is gitignored and never committed."
}
if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Create it: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt"
}

# Parse .env the way EnvironmentFile= does: KEY=value, one per line, blanks and
# #-comments skipped, only the first `=` splits, surrounding quotes stripped.
# No expansion of $VAR or backticks -- a password is a literal.
foreach ($line in Get-Content -LiteralPath $envFile -Encoding utf8) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) { continue }
    $split = $trimmed.IndexOf("=")
    if ($split -lt 1) { continue }
    $key = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim()
    if ($value.Length -ge 2) {
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    Set-Item -Path "Env:$key" -Value $value
}

# The package is not installed (review item [r7]); PYTHONPATH is what makes
# `python -m nwnbot` resolve, mirroring the systemd unit.
$env:PYTHONPATH = $repo
$env:PYTHONUNBUFFERED = "1"

Set-Location -LiteralPath $repo
# @() is load-bearing: a one-word command splits to a bare string, and
# splatting a string passes it one CHARACTER at a time.
$argv = @($Command -split "\s+" | Where-Object { $_ -ne "" })
& $python -m nwnbot @argv
exit $LASTEXITCODE
