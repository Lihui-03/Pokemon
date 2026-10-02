$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    py -3 -m venv .venv
    & $python -m pip install -r requirements.txt
}
& $python -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -r requirements.txt
}
if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Write-Error "Copy .env.example to .env and set DISCORD_TOKEN before starting."
}
$envText = Get-Content (Join-Path $PSScriptRoot ".env") -Raw
if ($envText -match '(?m)^\s*AUTO_PURCHASE\s*=\s*true\s*$' -and $envText -match '(?m)^\s*PURCHASE_DRY_RUN\s*=\s*false\s*$') {
    Write-Host "AUTO_PURCHASE is on and dry run is off. The bot can place one real Target order."
}
& $python bot.py
