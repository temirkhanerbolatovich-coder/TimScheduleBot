@echo off
chcp 65001 >nul
setlocal
title TimScheduleBot
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
    echo Не найдено локальное окружение Python. Его нужно создать в этой папке.
    echo Для восстановления: python -m venv .venv
    echo Затем: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -u run_bot.py
echo.
echo Бот остановлен. Для повторного запуска открой START_BOT.cmd.
pause
