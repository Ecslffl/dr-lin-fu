@echo off
chcp 65001 >nul 2>&1
title 日语字幕生成器
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PYEXE="

REM --- 1. 项目自带完整 Python（含 tkinter，推荐） ---
set "PROJ_PY=%CD%\python\python.exe"
if exist "%PROJ_PY%" (
    "%PROJ_PY%" -c "import tkinter, faster_whisper, requests" >nul 2>&1
    if not errorlevel 1 set "PYEXE=%PROJ_PY%"
)

REM --- 2. 项目虚拟环境 .venv ---
if not defined PYEXE (
    set "VENV_PY=%CD%\.venv\Scripts\python.exe"
    if exist "!VENV_PY!" (
        "!VENV_PY!" -c "import tkinter, faster_whisper, requests" >nul 2>&1
        if not errorlevel 1 set "PYEXE=!VENV_PY!"
    )
)

REM --- 3. PATH 中自带依赖的 python ---
if not defined PYEXE (
    python -c "import tkinter, faster_whisper, requests" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

REM --- 4. 再试 py launcher ---
if not defined PYEXE (
    for /f "delims=" %%E in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%E"
)

REM --- 5. 都没有则提示先装环境 ---
if not defined PYEXE (
    echo.
    echo  [X] 未找到可用的 Python 环境（需带 tkinter 且已装 faster-whisper）。
    echo      请先双击运行 安装依赖.bat 完成环境搭建。
    echo.
    pause
    exit /b 1
)

echo [*] 使用 Python: %PYEXE%
echo.
"%PYEXE%" -m jp_subtitle_app.main
if errorlevel 1 (
    echo.
    echo  [X] 程序异常退出，请查看上方日志。
    pause
)
endlocal
