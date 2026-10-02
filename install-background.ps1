# Registers a logon task so the restock bot keeps running on this PC.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "Copy .env.example to .env and set DISCORD_TOKEN first."
}
$tokenLine = Get-Content $envFile | Where-Object { $_ -match '^\s*DISCORD_TOKEN\s*=\s*\S+' }
if (-not $tokenLine) {
    Write-Error "DISCORD_TOKEN is empty in .env."
}
$autoLine = Get-Content $envFile | Where-Object { $_ -match '^\s*AUTO_PURCHASE\s*=\s*true\s*(#.*)?$' }
if ($autoLine) {
    Write-Error "AUTO_PURCHASE is on. Start the bot with start-bot.ps1 in a visible window so Chrome can open. A hidden logon task was not registered."
}

$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    py -3 -m venv .venv
    & (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") -m pip install -r requirements.txt
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "bot.py" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
    -TaskName "Pokemon30RestockBot" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Pokemon 30th Celebration restock Discord alerts" `
    -Force | Out-Null

Start-ScheduledTask -TaskName "Pokemon30RestockBot"
Write-Output "Pokemon30RestockBot is registered and started. Logs: logs\bot.log"
