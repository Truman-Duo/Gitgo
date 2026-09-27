@echo off
setlocal EnableExtensions
title Gitgo Dashboard

rem The formal dashboard is a color terminal product. Some launchers leak
rem NO_COLOR=1 / TERM=dumb into child consoles, making the real Ink tree look
rem like an unstyled smoke page. Keep one explicit accessibility opt-out.
if /i "%GITGO_NO_COLOR%"=="1" (
    set "NO_COLOR=1"
    set "FORCE_COLOR=0"
) else (
    set "NO_COLOR="
    if /i "%TERM%"=="dumb" set "TERM=xterm-256color"
    if not defined FORCE_COLOR set "FORCE_COLOR=3"
)
chcp 65001 >nul
set "PYTHONUTF8=1"

cd /d "%~dp0"
set "GITGO_ROOT=%CD%"

if not defined GITGO_BUN set "GITGO_BUN=%USERPROFILE%\.bun\bun.exe"
if not defined GITGO_PYTHON if exist "%USERPROFILE%\.gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%USERPROFILE%\.gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined GITGO_PYTHON if exist "%TEMP%\gitgo-realapi-python\python.exe" set "GITGO_PYTHON=%TEMP%\gitgo-realapi-python\python.exe"

if not exist "%GITGO_BUN%" (
    echo [gitgo] Bun was not found:
    echo          %GITGO_BUN%
    echo [gitgo] Set GITGO_BUN to the full path of bun.exe and try again.
    pause
    exit /b 1
)

if not defined GITGO_PYTHON (
    echo [gitgo] Python was not found:
    echo          %GITGO_PYTHON%
    echo [gitgo] Set GITGO_PYTHON to the full path of python.exe and try again.
    pause
    exit /b 1
)
if not exist "%GITGO_PYTHON%" (
    echo [gitgo] Python was not found:
    echo          %GITGO_PYTHON%
    echo [gitgo] Set GITGO_PYTHON to the full path of python.exe and try again.
    pause
    exit /b 1
)

if not exist "%GITGO_ROOT%\cli\dashboard\src\main.tsx" (
    echo [gitgo] Dashboard entry point was not found under:
    echo          %GITGO_ROOT%
    pause
    exit /b 1
)

echo [gitgo] Starting native Dashboard
echo [gitgo] Root:    %GITGO_ROOT%
echo [gitgo] Bun:     %GITGO_BUN%
echo [gitgo] Python:  %GITGO_PYTHON%
if /i "%GITGO_NO_COLOR%"=="1" (
    echo [gitgo] Color:   disabled by GITGO_NO_COLOR=1
) else (
    echo [gitgo] Color:   enabled
)
echo.

"%GITGO_BUN%" run "%GITGO_ROOT%\cli\dashboard\src\main.tsx" %*
set "GITGO_EXIT=%ERRORLEVEL%"

if not "%GITGO_EXIT%"=="0" (
    echo.
    echo [gitgo] Dashboard exited with code %GITGO_EXIT%.
    pause
)

exit /b %GITGO_EXIT%
