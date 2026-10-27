@echo off
chcp 65001 >nul 2>&1
title 日语字幕生成器 - 安装依赖
setlocal
cd /d "%~dp0"

set "PYEXE="

REM --- 1. 项目自带完整 Python（含 tkinter） ---
if exist "%CD%\python\python.exe" set "PYEXE=%CD%\python\python.exe"

REM --- 2. 项目虚拟环境 .venv ---
if not defined PYEXE (
    if exist "%CD%\.venv\Scripts\python.exe" set "PYEXE=%CD%\.venv\Scripts\python.exe"
)

REM --- 3. PATH 中带 tkinter 的 python（取最后一个命中的） ---
if not defined PYEXE (
    for /f "delims=" %%E in ('where python 2^>nul') do (
        "%%E" -c "import tkinter" >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%E"
    )
)

REM --- 4. py launcher ---
if not defined PYEXE (
    for /f "delims=" %%E in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%E"
)

if not defined PYEXE (
    echo.
    echo  [X] 未找到带 tkinter 的 Python。
    echo      请到 python.org 下载安装 Python 3.10+（安装时勾选 tcl/tk 和 pip）。
    echo.
    pause
    exit /b 1
)
echo [*] 使用 Python: %PYEXE%

echo [*] 安装依赖（首次约需几分钟，含 Whisper 加速库）...
"%PYEXE%" -m pip install --upgrade pip
echo [*] 使用清华 PyPI 镜像安装（本机默认镜像可能不可达）...
"%PYEXE%" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 (
    echo [*] 清华镜像失败，改用中科大镜像重试 ...
    "%PYEXE%" -m pip install --index-url https://mirrors.ustc.edu.cn/pypi/simple/ -r requirements.txt
)
if errorlevel 1 (
    echo [*] 镜像均失败，尝试默认 PyPI ...
    "%PYEXE%" -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo.
    echo  [X] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo  [OK] 依赖安装完成。双击 启动字幕生成器.bat 即可运行。
pause
