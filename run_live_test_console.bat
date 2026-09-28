@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "GITGO_ROOT=%CD%"
if not defined GITGO_BUN set "GITGO_BUN=%USERPROFILE%\.bun\bun.exe"
if not defined GITGO_PYTHON set "GITGO_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not exist "%GITGO_BUN%" (
  echo [gitgo-live] Bun not found: %GITGO_BUN%
  pause
  exit /b 1
)
if not exist "%GITGO_PYTHON%" (
  echo [gitgo-live] Python not found: %GITGO_PYTHON%
  pause
  exit /b 1
)

echo [gitgo-live] Starting local console at http://127.0.0.1:43121/
"%GITGO_BUN%" run "%GITGO_ROOT%\cli\dashboard\src\liveTestServer.ts"
set "GITGO_EXIT=%ERRORLEVEL%"
if not "%GITGO_EXIT%"=="0" pause
exit /b %GITGO_EXIT%
