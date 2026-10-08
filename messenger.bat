@echo off
rem Кнопка «Мессенджер»: двойной клик открывает «Диалоги» с сервера в браузере.
rem   messenger.bat          - открыть
rem   messenger.bat -Stop    - остановить «Диалоги» на сервере
rem   messenger.bat -SetupKey - один раз настроить вход без пароля
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0messenger.ps1" %*
echo.
pause
