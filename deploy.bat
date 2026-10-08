@echo off
rem Заливает весь код на сервер (двойной клик). Спросит пароль root (если не настроен вход по ключу).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
echo.
pause
