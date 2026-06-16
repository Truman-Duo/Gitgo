@echo off
C:\Users\Duo\AppData\Local\Programs\Python\Python312\python -c "from backend.core.config import ConfigManager; from backend.core.daemon import run_daemon; cfg=ConfigManager.load(); proj=next(p for p in cfg.projects if p.name=='%1'); print(f'Daemon: %1'); run_daemon(cfg, proj, trial_interval=9999, debounce_sec=2.0)"
