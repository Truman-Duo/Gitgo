@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -B service.py --port 8767 --open
goto done
:python
python -B service.py --port 8767 --open
:done
if errorlevel 1 pause
endlocal
