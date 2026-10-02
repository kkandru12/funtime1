@echo off
REM Single-window launcher: bridge + 0DTE algo + ES algo, all in one console.
REM Default is dry-run (no orders). For paper/demo orders, use start_live.bat
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo No venv found. Run setup.bat first.
    pause
    exit /b 1
)
venv\Scripts\python run_all.py --dry-run
pause
