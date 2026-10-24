@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\release_windows.ps1" -BuildOnly -ManagedUpdate %*
set "GITGO_PACK_EXIT=%ERRORLEVEL%"
if not "%GITGO_PACK_EXIT%"=="0" echo [gitgo] Packaging failed. Review the error above.
if not defined GITGO_PACK_NO_PAUSE pause
endlocal & exit /b %GITGO_PACK_EXIT%
