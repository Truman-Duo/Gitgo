@echo off
setlocal EnableExtensions
title Gitgo - repeatable terminal test
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
if not defined GITGO_PYTHON if exist "%USERPROFILE%\.gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%USERPROFILE%\.gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined GITGO_BUN set "GITGO_BUN=%USERPROFILE%\.bun\bun.exe"
if not exist "%GITGO_PYTHON%" (
    echo [gitgo-test] Python not found. Set GITGO_PYTHON to python.exe.
    pause
    exit /b 1
)
"%GITGO_PYTHON%" "%~dp0scripts\terminal_test_launcher.py"
set "GITGO_TEST_EXIT=%ERRORLEVEL%"
if not "%GITGO_TEST_EXIT%"=="0" pause
exit /b %GITGO_TEST_EXIT%
