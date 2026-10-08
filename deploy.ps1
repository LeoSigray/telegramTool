# deploy.ps1 - заливает ВЕСЬ код на сервер одной командой.
#
#   .\deploy.ps1
#
# Что заливается: .py, .sh, .md, .html, requirements.txt, .env.example из проекта.
# Что НЕ заливается никогда: .env (ключи), sessions/, accounts/*.txt, data/database.db, data/*.xlsx,
# data/purchases/, .venv, .git, desktop/, foricons/, а также ваши тексты рассылки
# (data/static_message.py, data/vk_static_message.py) и список каналов (data/warmup_channels.txt):
# их вы правите сами, и деплой не должен их затирать.
#
# Нужен пароль root (спросит дважды: при копировании и при распаковке). Через ключ - без пароля
# (настройка: messenger.bat -SetupKey).

param([switch]$DryRun)

# ---- настройки ----
$ServerIp  = "80.74.30.16"
$User      = "root"
$RemoteDir = "/home/tg/telegramTool"
$Owner     = "tg:tg"
# -------------------

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$target = "$User@$ServerIp"
# Отдельный ключ только для этого сервера (создаётся ярлыком «Доступ к серверу (один раз)»)
$KeyFile = Join-Path $HOME ".ssh\telegramtool_ed25519"
$KeyOpts = @()
if (Test-Path $KeyFile) { $KeyOpts = @("-i", $KeyFile, "-o", "IdentitiesOnly=yes") }

$skipDirs  = @(".venv", ".git", "__pycache__", "desktop", "sessions", "foricons", "node_modules", "tdata_accounts", "purchases")
$skipFiles = @("data/static_message.py", "data/vk_static_message.py", "data/warmup_channels.txt")

$root = (Get-Location).Path.TrimEnd('\') + '\'
$files = Get-ChildItem -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length).Replace('\', '/')
    $parts = $rel.Split("/")
    $ext = $_.Extension.ToLower()
    $okExt = ($ext -in ".py", ".sh", ".md", ".html") -or ($rel -in "requirements.txt", ".env.example")
    $badDir = $false
    if ($parts.Length -gt 1) { foreach ($d in $parts[0..($parts.Length - 2)]) { if ($d -in $skipDirs) { $badDir = $true } } }
    if ($okExt -and -not $badDir -and ($rel -notin $skipFiles) -and ($rel -notlike "accounts/*.txt") -and ($rel -ne ".env")) { $rel }
}

if (-not $files) { throw "Не нашёл файлов для заливки." }
Write-Host ("К заливке: {0} файлов." -f $files.Count) -ForegroundColor Cyan

$list = Join-Path $env:TEMP "deploy_files.txt"
$arc  = Join-Path $env:TEMP "telegramtool_code.tgz"
Set-Content -Path $list -Value $files -Encoding ascii
& "$env:SystemRoot\System32\tar.exe" -czf $arc -T $list

if ($DryRun) {
    Write-Host "DryRun: архив $arc собран, на сервер не отправляю." -ForegroundColor Yellow
    return
}

Write-Host "Копирую на сервер..." -ForegroundColor Cyan
& scp @KeyOpts $arc "${target}:/tmp/telegramtool_code.tgz"
if ($LASTEXITCODE -ne 0) { Write-Host "НЕ ЗАЛИЛОСЬ: не удалось скопировать архив на сервер (пароль/ключ/связь). Код на сервере НЕ обновлён." -ForegroundColor Red; return }
Write-Host "Распаковываю и выставляю права..." -ForegroundColor Cyan
& ssh @KeyOpts $target "tar -xzf /tmp/telegramtool_code.tgz -C $RemoteDir && rm /tmp/telegramtool_code.tgz && chown -R ${Owner} $RemoteDir && chmod +x $RemoteDir/*.sh"
if ($LASTEXITCODE -ne 0) { Write-Host "НЕ ЗАЛИЛОСЬ: на сервере не удалось распаковать архив. Код на сервере мог остаться старым." -ForegroundColor Red; return }
# Мессенджер - постоянная служба: перезапускаем её, чтобы она подхватила новый код (пропущенные за секунды ответы он догонит сам)
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$svc = & ssh @KeyOpts $target "if systemctl is-enabled telegramtool-inbox >/dev/null 2>&1; then systemctl restart telegramtool-inbox && echo restarted; fi" 2>&1
$ErrorActionPreference = $prev
if ($svc -contains "restarted") { Write-Host "Мессенджер на сервере перезапущен с новым кодом." -ForegroundColor Green }
Write-Host "Готово: код на сервере обновлён. Чтобы изменения подействовали на прогрев/рассылку, перезапусти программу в окне tmux." -ForegroundColor Green
