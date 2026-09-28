@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not defined GITGO_PYTHON if exist "%USERPROFILE%\.gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%USERPROFILE%\.gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON (
  echo Gitgo Python runtime not found. Set GITGO_PYTHON to python.exe. 1>&2
  exit /b 2
)

"%GITGO_PYTHON%" -u mcp_control_server.py
