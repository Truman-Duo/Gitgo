@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "GITGO_ROOT=%CD%"
if not defined GITGO_BUN set "GITGO_BUN=%USERPROFILE%\.bun\bun.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined GITGO_PYTHON if exist "%LOCALAPPDATA%\Gitgo\runtime\python\python.exe" set "GITGO_PYTHON=%LOCALAPPDATA%\Gitgo\runtime\python\python.exe"
if not defined GITGO_PYTHON if exist "%TEMP%\gitgo-realapi-python\python.exe" set "GITGO_PYTHON=%TEMP%\gitgo-realapi-python\python.exe"
if not defined GITGO_PROJECT set "GITGO_PROJECT=gitgo"

if not exist "%GITGO_BUN%" (
    echo [gitgo] Bun was not found: %GITGO_BUN%
    pause
    exit /b 1
)
if not defined GITGO_PYTHON (
    echo [gitgo] Python was not found: %GITGO_PYTHON%
    pause
    exit /b 1
)
if not exist "%GITGO_PYTHON%" (
    echo [gitgo] Python was not found: %GITGO_PYTHON%
    pause
    exit /b 1
)

echo [gitgo] Starting Trace Inspector for project %GITGO_PROJECT%
"%GITGO_BUN%" run "%GITGO_ROOT%\cli\dashboard\src\traceMain.tsx" --project "%GITGO_PROJECT%" %*
set "GITGO_EXIT=%ERRORLEVEL%"
if not "%GITGO_EXIT%"=="0" pause
exit /b %GITGO_EXIT%
