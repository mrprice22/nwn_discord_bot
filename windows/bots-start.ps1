# Start the LLM server and the Discord sync bot, and re-enable them at boot.
#
# The counterpart of windows\bots-stop.ps1. Re-enables the scheduled tasks that
# stop disabled, so the pair survives the next reboot again.
#
# NWNBOT_DRY_RUN is NOT touched here, by either script. It lives in .env and it
# is the live/not-live switch: starting the bot must never be the thing that
# arms it. Same rule the systemd unit and the Task Scheduler XML both follow.

$ErrorActionPreference = "Continue"

function Start-Task($name, $label) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "  $label : no scheduled task registered yet - see windows\README.md"
        return $false
    }
    Enable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
    Start-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Write-Host "  $label : enabled and started"
    return $true
}

Write-Host "Starting..."
Start-Task "nwnbot-llm" "LLM server" | Out-Null
Start-Task "nwnbot"     "Discord bot" | Out-Null

Write-Host ""
Write-Host "Waiting for the model to load (~19 GB, usually under a minute)..."
$ok = $false
foreach ($i in 1..40) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:8080/v1/models" -TimeoutSec 4 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Write-Host "  still loading... ($($i * 5)s)"
}

if ($ok) {
    Write-Host ""
    Write-Host "LLM server ready on http://127.0.0.1:8080"
} else {
    Write-Host ""
    Write-Host "LLM server did not answer within 200s. The bot still runs without"
    Write-Host "it - duplicate scoring falls back to the token scorer and says so."
}

$bot = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -like "*nwnbot*" }
Write-Host ("Discord bot: " + $(if ($bot) { "running" } else { "not running" }))
