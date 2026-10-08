# messenger.ps1 - кнопка «Мессенджер»: открывает «Диалоги» с сервера в браузере.
#
#   .\messenger.ps1              открыть мессенджер
#   .\messenger.ps1 -Status      показать: работает ли мессенджер на сервере, включён ли автозапуск, сколько аккаунтов слушает
#   .\messenger.ps1 -Off         выключить мессенджер насовсем (остановить и убрать из автозапуска; спросит подтверждение)
#   .\messenger.ps1 -Stop        остановить до перезагрузки сервера / до следующего запуска кнопкой (обычно НЕ нужно, см. ниже)
#   .\messenger.ps1 -SetupKey    один раз: настроить вход по ключу, чтобы не вводить пароль root каждый раз
#
# Мессенджер на сервере - постоянная служба: работает всегда (и после перезагрузки сервера), сам записывает ответы
# на рассылку, даже когда ноутбук выключен, и сам отдаёт аккаунты прогреву/консольной рассылке, а потом забирает
# обратно и догоняет всё, что пришло за это время. При первом запуске кнопка сама установит службу.
# Удобнее всего запускать двойным кликом по messenger.bat.

param([switch]$Stop, [switch]$SetupKey, [switch]$Status, [switch]$Off)

# ---- настройки ----
$ServerIp = "80.74.30.16"
$User     = "root"
$Port     = 9000          # порт на ноутбуке и на сервере (на сервере слушает только 127.0.0.1)
# -------------------

$ErrorActionPreference = "Stop"
$target = "$User@$ServerIp"
# Отдельный ключ только для этого сервера (чужие ключи от других проектов сюда не подходят)
$KeyFile = Join-Path $HOME ".ssh\telegramtool_ed25519"
$KeyOpts = @()
if (Test-Path $KeyFile) { $KeyOpts = @("-i", $KeyFile, "-o", "IdentitiesOnly=yes") }

# Выполняет команду на сервере. Обычный вывод возвращает, сообщения об ошибках/предупреждения печатает серым
# (иначе PowerShell 5.1 считал бы любую строку из stderr ssh аварией).
function Invoke-Remote([string]$cmd) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $o = & ssh @KeyOpts $target $cmd 2>&1 } finally { $ErrorActionPreference = $prev }
    foreach ($l in $o) {
        if ($l -is [System.Management.Automation.ErrorRecord]) { Write-Host ("  " + $l.ToString()) -ForegroundColor DarkGray }
        else { $l }
    }
}

if ($SetupKey) {
    if (-not (Test-Path $KeyFile)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $KeyFile) | Out-Null
        & ssh-keygen -q -t ed25519 -N '""' -C "telegramtool-server" -f $KeyFile
    }
    $pub = (Get-Content "$KeyFile.pub" -Raw).Trim()
    $inst = @'
mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys
if [ -n "$(tail -c1 ~/.ssh/authorized_keys)" ]; then echo >> ~/.ssh/authorized_keys; fi
grep -qxF 'PUBKEY' ~/.ssh/authorized_keys || echo 'PUBKEY' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo KEY_INSTALLED
'@
    $inst = ($inst -replace "`r", "") -replace "PUBKEY", $pub
    $b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($inst))
    Write-Host "Сейчас спросит пароль root (при вводе символы не видны - это нормально). Введи его и нажми Enter." -ForegroundColor Yellow
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $r = & ssh -o PubkeyAuthentication=no $target "echo $b | base64 -d | bash"
    $ErrorActionPreference = $prev
    if (-not ($r -contains "KEY_INSTALLED")) {
        Write-Host "НЕ ПОЛУЧИЛОСЬ: ключ не установлен (неверный пароль или нет связи с сервером). Запусти ярлык ещё раз." -ForegroundColor Red
        return
    }
    # проверка: заходим уже по ключу, БЕЗ пароля
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $t = & ssh -i $KeyFile -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 $target "echo KEY_OK" 2>&1
    $ErrorActionPreference = $prev
    if ($t -contains "KEY_OK") {
        Write-Host "ГОТОВО: вход по ключу работает, пароль больше не нужен." -ForegroundColor Green
    } else {
        Write-Host "Ключ записан, но вход по ключу не прошёл. Ответ сервера:" -ForegroundColor Red
        $t | ForEach-Object { Write-Host "  $_" }
    }
    return
}

if ($Off) {
    Write-Host "Выключить мессенджер НАСОВСЕМ?" -ForegroundColor Yellow
    Write-Host "Он остановится и не запустится сам даже после перезагрузки сервера. Пока он выключен, ответы на рассылку" -ForegroundColor Yellow
    Write-Host "не записываются (Telegram их хранит, и после включения за последние 7 суток они подтянутся сами)." -ForegroundColor Yellow
    $a = Read-Host "Введи YES и нажми Enter, чтобы выключить (что угодно другое - отмена)"
    if ($a -ne "YES") { Write-Host "Отмена, мессенджер не тронут." -ForegroundColor Green; return }
    Invoke-Remote "systemctl disable --now telegramtool-inbox; su - tg -c 'tmux kill-session -t inbox 2>/dev/null'; echo off" | Out-Null
    Write-Host "Мессенджер выключен и убран из автозапуска." -ForegroundColor Green
    Write-Host "Включить обратно: двойной клик по «Мессенджер (Диалоги)»." -ForegroundColor DarkGray
    return
}

if ($Stop) {
    Invoke-Remote "systemctl stop telegramtool-inbox; su - tg -c 'tmux kill-session -t inbox 2>/dev/null'; echo stopped" | Out-Null
    Write-Host "Мессенджер на сервере остановлен до перезагрузки сервера или до следующего запуска кнопкой «Мессенджер»." -ForegroundColor Yellow
    Write-Host "Пока он остановлен, ответы НЕ записываются в реальном времени (за 7 суток подтянутся при запуске)." -ForegroundColor Yellow
    Write-Host "Консольной рассылке/прогреву останавливать его НЕ нужно: аккаунты отдаются им автоматически." -ForegroundColor DarkGray
    return
}

if ($Status) {
    $probe = @'
echo "active=$(systemctl is-active telegramtool-inbox 2>&1)"
echo "enabled=$(systemctl is-enabled telegramtool-inbox 2>&1)"
echo "since=$(systemctl show -p ActiveEnterTimestamp --value telegramtool-inbox 2>/dev/null)"
echo "restarts=$(systemctl show -p NRestarts --value telegramtool-inbox 2>/dev/null)"
echo "health=$(curl -s -m 5 http://127.0.0.1:9000/health 2>/dev/null)"
echo "uptime=$(uptime -p 2>/dev/null)"
'@
    $b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($probe -replace "`r", "")))
    $kv = @{}
    Invoke-Remote "echo $b | base64 -d | bash" | ForEach-Object {
        if ($_ -is [string] -and $_ -match "^(\w+)=(.*)$") { $kv[$Matches[1]] = $Matches[2].Trim() }
    }
    if (-not $kv.ContainsKey("active")) { Write-Host "Не удалось связаться с сервером (проверь интернет/пароль/ключ)." -ForegroundColor Red; return }
    Write-Host ""
    if ($kv["active"] -eq "active") { Write-Host "  Мессенджер:  РАБОТАЕТ" -ForegroundColor Green }
    else { Write-Host ("  Мессенджер:  НЕ РАБОТАЕТ (" + $kv["active"] + ")") -ForegroundColor Red }
    if ($kv["enabled"] -eq "enabled") { Write-Host "  Автозапуск:  включён - стартует сам после перезагрузки сервера и после сбоя" -ForegroundColor Green }
    else { Write-Host ("  Автозапуск:  ВЫКЛЮЧЕН (" + $kv["enabled"] + ") - после перезагрузки сервера сам не запустится") -ForegroundColor Yellow }
    if ($kv["since"]) { Write-Host ("  Запущен с:   " + $kv["since"] + "  (время сервера)") }
    if ($kv["restarts"] -match "^\d+$") {
        $n = [int]$kv["restarts"]
        $col = if ($n -gt 5) { "Yellow" } else { "Gray" }
        Write-Host ("  Перезапусков после сбоев: $n") -ForegroundColor $col
    }
    if ($kv["health"] -match '"active_accounts"\s*:\s*(\d+)') {
        Write-Host ("  Слушает аккаунтов сейчас: " + $Matches[1] + "  (остальные заняты прогревом/рассылкой или не рабочие)")
    } elseif ($kv["active"] -eq "active") {
        Write-Host "  Слушает аккаунтов: пока неизвестно (идёт запуск - подожди минуту и повтори)" -ForegroundColor Yellow
    }
    if ($kv["uptime"]) { Write-Host ("  Сервер включён: " + $kv["uptime"]) -ForegroundColor DarkGray }
    Write-Host ""
    if ($kv["active"] -eq "active" -and $kv["enabled"] -eq "enabled") {
        Write-Host "  Всё в порядке: ответы записываются, даже когда ноутбук выключен." -ForegroundColor Green
    } else {
        Write-Host "  Чтобы включить: двойной клик по «Мессенджер (Диалоги)»." -ForegroundColor Yellow
    }
    return
}

Write-Host "Проверяю мессенджер на сервере (первый раз установит постоянную службу)..." -ForegroundColor Cyan
# Скрипт установки/запуска отправляем на сервер прямо в команде (base64), отдельно заливать его не нужно
$script = (Get-Content (Join-Path $PSScriptRoot "install_inbox.sh") -Raw -Encoding UTF8) -replace "`r", ""
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
$out = Invoke-Remote "echo $b64 | base64 -d | bash"
$token = ($out | Where-Object { $_ -is [string] -and $_.Trim() -ne "" } | Select-Object -Last 1)
if ($token) { $token = $token.Trim() }
if (-not $token -or $token -match "[\s:]") {
    Write-Host "Не удалось получить токен. Ответ сервера:" -ForegroundColor Red
    $out | ForEach-Object { Write-Host "  $_" }
    Write-Host "Если ошибка про uvicorn/api.server - залей актуальный код: deploy.bat." -ForegroundColor Yellow
    return
}

# туннель: ноутбук 127.0.0.1:9000 -> сервер 127.0.0.1:9000
$busy = $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (-not $busy) {
    Write-Host "Открываю туннель (окно можно свернуть, не закрывай его, пока работаешь с мессенджером)..." -ForegroundColor Cyan
    Start-Process ssh -ArgumentList (@("-N", "-L", "${Port}:127.0.0.1:${Port}", "-o", "ServerAliveInterval=30") + $KeyOpts + @($target))
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { break }
    }
} else {
    Write-Host "Порт $Port уже занят (туннель открыт?) - использую его." -ForegroundColor DarkGray
}

Start-Process "http://127.0.0.1:$Port/inbox?token=$token"
Write-Host "Открыл в браузере. Мессенджер на сервере работает постоянно: ответы записываются, даже когда это окно закрыто." -ForegroundColor Green
