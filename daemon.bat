@echo off
setlocal
set "GITGO_DAEMON_PYTHON=%GITGO_PYTHON%"
if not defined GITGO_DAEMON_PYTHON set "GITGO_DAEMON_PYTHON=%USERPROFILE%\.gitgo\runtime\python\python.exe"
if not exist "%GITGO_DAEMON_PYTHON%" set "GITGO_DAEMON_PYTHON=%LOCALAPPDATA%\Gitgo\runtime\python\python.exe"
if not exist "%GITGO_DAEMON_PYTHON%" (
  echo Gitgo Python runtime not found. Configure GITGO_PYTHON or install the runtime.
  exit /b 2
)
"%GITGO_DAEMON_PYTHON%" -c "from backend.core.config import ConfigManager; from backend.core.daemon import run_daemon; cfg=ConfigManager.load(); proj=next(p for p in cfg.projects if p.name=='%1'); print(f'Daemon: %1'); run_daemon(cfg, proj, trial_interval=9999, debounce_sec=2.0)"
