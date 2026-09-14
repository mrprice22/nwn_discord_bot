# Stop the LLM server and the Discord sync bot, and keep them stopped.
#
# For reclaiming the box to game on: the model holds ~20 GB of RAM and most of
# the VRAM while it is up. Run windows\bots-start.ps1 to bring both back.
#
# Disables the scheduled tasks as well as killing the processes, so a reboot
# mid-session does not quietly start them again underneath you. That is the
# whole point - stopping a process you did not disable is a temporary stop.

$ErrorActionPreference = "Continue"

function Stop-Task($name) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "  $name : no scheduled task (skipping)"; return }
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
    Write-Host "  $name : stopped and disabled"
}

Write-Host "Stopping scheduled tasks..."
Stop-Task "nwnbot"
Stop-Task "nwnbot-llm"

Write-Host "Stopping processes..."
foreach ($p in @("llama-server")) {
    $procs = Get-Process $p -ErrorAction SilentlyContinue
    if (-not $procs) { Write-Host "  $p : not running"; continue }
    $gb = [math]::Round(($procs | Measure-Object WorkingSet64 -Sum).Sum / 1GB, 1)
    $procs | Stop-Process -Force
    # Wait for it to actually go. Releasing ~19 GB is not instant, and the next
    # thing you do after this script is launch a game - reporting "freed" while
    # the memory is still held would be a lie at the worst moment.
    $gone = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Process $p -ErrorAction SilentlyContinue)) { $gone = $true; break }
    }
    if ($gone) {
        Write-Host "  $p : stopped, freed ~$gb GB"
    } else {
        Write-Host "  $p : asked to stop but STILL RUNNING after 15s - check Task Manager"
    }
}

# The bot is a python process, so match on its command line rather than its
# name - killing every python.exe on the box would be a rude way to free 200 MB.
$bot = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -like "*nwnbot*" }
if ($bot) {
    $bot | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "  nwnbot : stopped"
} else {
    Write-Host "  nwnbot : not running"
}

Write-Host ""
Write-Host "Both stopped. The GPU and RAM are yours."
Write-Host "Run windows\bots-start.ps1 when you are done."
