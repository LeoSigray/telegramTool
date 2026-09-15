# run_preview.ps1 - starts backend + serves the dashboard page in ONE command,
# so you can view the new dashboards in a browser without building the Tauri app.
#
#   .\run_preview.ps1
#
# Open the URL it prints. Ctrl+C stops both processes.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "No .venv found. Create it: python -m venv .venv ; .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# static file server (the dashboard HTML) - runs hidden in the background
$srcDir = Join-Path $PSScriptRoot "desktop\telegramtool-dashboard\src"
$static = Start-Process -PassThru -WindowStyle Hidden $py `
    -ArgumentList "-m", "http.server", "8899", "--bind", "127.0.0.1" `
    -WorkingDirectory $srcDir

try {
    $env:API_TOKEN = "devtoken"
    Write-Host ""
    Write-Host "  Dashboard:  http://127.0.0.1:8899/index.html?token=devtoken" -ForegroundColor Green
    Write-Host "  Ctrl+C to stop" -ForegroundColor DarkGray
    Write-Host ""
    & $py -m uvicorn api.server:app --host 127.0.0.1 --port 9000 --log-level warning
}
finally {
    if ($static -and -not $static.HasExited) {
        Stop-Process -Id $static.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "stopped" -ForegroundColor DarkGray
}
