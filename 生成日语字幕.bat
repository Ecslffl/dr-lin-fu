﻿@echo off
chcp 65001 >nul 2>&1
title 日语字幕生成器

REM ============================================
REM  日语视频 -> 中日双语字幕 拖拽生成器
REM  把视频文件拖到这个 bat 图标上即可
REM  支持多文件批量拖拽
REM ============================================

set "PYTHON=C:\Users\FuLin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SCRIPT=C:\Users\FuLin\WorkBuddy\2026-10-06-23-01-13\ja-zh-subtitle-tool\generate_subtitle.py"

REM --- 环境检查 ---
if not exist "%PYTHON%" (
    echo [X] 找不到 Python 解释器:
    echo     %PYTHON%
    echo     请确认 .workbuddy Python venv 已初始化。
    pause
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo [X] 找不到字幕生成脚本:
    echo     %SCRIPT%
    echo     请确认 ja-zh-subtitle-tool 项目目录存在。
    pause
    exit /b 1
)

REM --- 无参数时显示用法 ---
if "%~1"=="" (
    echo ========================================
    echo   日语视频 - 中日字幕生成器
    echo ========================================
    echo.
    echo  用法：把视频文件拖拽到这个图标上
    echo  支持多文件批量拖拽
    echo.
    echo  输入：mp4 / mkv / avi / mov / wmv / flv / m4a / wav / mp3 ...
    echo  输出：视频同级目录生成同名 .srt 字幕
    echo  模式：中日双语（上行中文 / 下行日文）
    echo  翻译：LM Studio 本地大模型（需先启动服务端）
    echo.
    echo  首次运行会下载 Whisper large-v3 模型约 3GB，
    echo  之后从本地缓存加载。
    echo.
    pause
    exit /b 0
)

REM --- 批量处理拖拽的文件 ---
set "SUCCESS=0"
set "FAIL=0"

:loop_start
if "%~1"=="" goto loop_end

echo ========================================
echo  处理: %~nx1
echo  目录: %~dp1
echo ========================================
echo.

"%PYTHON%" "%SCRIPT%" -i "%~1" --mode bilingual --no-vad --no-condition --normalize dynaudnorm

if errorlevel 1 (
    echo.
    echo  [X] 失败: %~nx1
    set /a FAIL+=1
) else (
    echo.
    echo  [OK] 已生成: %~n1.srt
    set /a SUCCESS+=1
)
echo.

shift
goto loop_start

:loop_end

echo ========================================
echo  批量处理完成
echo  成功: %SUCCESS% 个  失败: %FAIL% 个
echo ========================================
echo.
echo  字幕文件与视频同名，PotPlayer 打开视频时自动加载。
echo  字幕样式调整: PotPlayer 右键 -^> 字幕 -^> 字幕样式
echo.
pause