@echo off
chcp 65001 >nul
title BetterAGI - MiniCPM-o 4.5 实时屏幕解说
setlocal enabledelayedexpansion

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo ======================================================================
echo          BetterAGI - MiniCPM-o 4.5 实时屏幕解说一键启动器
echo ======================================================================
echo.

:: 1. 查找 Python 环境
set "PYTHON_EXE="
if exist "..\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=..\.venv\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set "PYTHON_EXE=python"
    )
)

if "%PYTHON_EXE%"=="" (
    echo [错误] 未找到 Python 运行环境！请确认已配置 Python 或安装 .venv 依赖。
    pause
    exit /b 1
)

echo [*] 使用 Python 解释器: %PYTHON_EXE%
echo.
echo 提示：
echo 1. 直接按 [回车]：盯住整个电脑桌面屏幕进行实时解说；
echo 2. 输入窗口关键词（如：原神、UNDERTALE、Chrome）：只盯住该窗口解说。
echo.
set /p "TARGET_WIN=请输入要盯住的窗口标题关键词 (留空直接全屏): "

echo.
if "!TARGET_WIN!"=="" (
    echo [*] 启动模式: 盯着全屏幕
    "%PYTHON_EXE%" live_commentator.py
) else (
    echo [*] 启动模式: 盯着指定窗口 "!TARGET_WIN!"
    "%PYTHON_EXE%" live_commentator.py --window "!TARGET_WIN!"
)

echo.
echo [*] 程序已退出。
pause
