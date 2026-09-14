# Put "Bots: stop (gaming)" and "Bots: start" on the desktop.
#
# Run once. Both shortcuts point at scripts in this repo, so editing the script
# changes what the shortcut does - nothing is copied or frozen into the .lnk.

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($name, $script, $icon, $description) {
    $path = Join-Path $desktop "$name.lnk"
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = "powershell.exe"
    # -NoExit so the window stays up and you can read what it did. These are
    # run by hand, at the moment you want to know the answer.
    $sc.Arguments = "-NoProfile -NoExit -ExecutionPolicy Bypass -File `"$repo\windows\$script`""
    $sc.WorkingDirectory = $repo
    $sc.IconLocation = $icon
    $sc.Description = $description
    $sc.Save()
    Write-Host "  $path"
}

Write-Host "Creating desktop shortcuts:"
New-Shortcut "Bots - stop (free the GPU)" "bots-stop.ps1" `
    "$env:SystemRoot\System32\shell32.dll,27" `
    "Stop the LLM server and the Discord sync bot, and keep them stopped."
New-Shortcut "Bots - start" "bots-start.ps1" `
    "$env:SystemRoot\System32\shell32.dll,25" `
    "Start the LLM server and the Discord sync bot, and re-enable them at boot."

Write-Host ""
Write-Host "Done. Stopping also DISABLES the scheduled tasks, so a reboot"
Write-Host "mid-session will not bring them back underneath you."
