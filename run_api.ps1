# run_api.ps1 - start the HTTP API from PowerShell (Windows equivalent of run_api.sh).
#
#   .\run_api.ps1
#
# Listens on 0.0.0.0:9000. Ctrl+C to stop.
# Token: if API_TOKEN is not set (env or .env), api/auth.py generates one
# and stores it in ~/.telegramtool/token.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# pull .env into the environment if API_TOKEN is not already set
if (-not $env:API_TOKEN -and (Test-Path ".env")) {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*)\s*$') {
            $name = $matches[1]
            $val = $matches[2].Trim().Trim('"').Trim("'")
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                Set-Item -Path "Env:$name" -Value $val
            }
        }
    }
}

if (-not $env:API_TOKEN) {
    Write-Host "API_TOKEN not set - will be auto-generated in ~/.telegramtool/token" -ForegroundColor DarkGray
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$apiHost = if ($env:API_HOST) { $env:API_HOST } else { "0.0.0.0" }
$apiPort = if ($env:API_PORT) { $env:API_PORT } else { "9000" }

Write-Host ("API:  http://127.0.0.1:{0}" -f $apiPort) -ForegroundColor Green
Write-Host ("Inbox: http://127.0.0.1:{0}/inbox   Dashboard: http://127.0.0.1:{0}/dashboard" -f $apiPort) -ForegroundColor DarkGray

& $py -m uvicorn api.server:app --host $apiHost --port $apiPort --reload
