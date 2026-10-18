@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    where python >nul 2>nul && (python -m venv .venv) || (py -3 -m venv .venv)
)
if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" run.py
pause
