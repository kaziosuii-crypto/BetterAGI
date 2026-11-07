@echo off
chcp 65001 >nul
title BetterAGI TUI
cd /d E:\BetterAGI\agent
set UV_LINK_MODE=copy
set PYTHONIOENCODING=utf-8
set BETTERAGI_DEV=1
uv run --no-project --with httpx --with pyyaml --with pillow --with websockets python -X utf8 tui.py --game undertale --restart --port 8791
echo.
echo [TUI exited with code %ERRORLEVEL%]
pause
