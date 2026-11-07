@echo off
rem Launch the BetterAGI TUI in its own console window. ASCII only.
chcp 65001 >nul
cd /d E:\BetterAGI\agent
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
title lumi
"E:\BetterAGI\Amaidesu\.venv\Scripts\python.exe" tui.py --goal-file E:\BetterAGI\_diag\goal1.txt --port 8791 %*
echo.
echo [lumi exited]
pause
