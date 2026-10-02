@echo off
REM Single-window launcher with LIVE order transmission.
REM algo -> IBKR paper account. algo_es -> MT5 demo account.
REM There is no live-money path in this code.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo No venv found. Run setup.bat first.
    pause
    exit /b 1
)
echo ============================================================
echo  LIVE MODE: orders will transmit to IBKR paper + MT5 demo.
echo  Press Ctrl+C within 10 seconds to abort.
echo ============================================================
timeout /t 10
venv\Scripts\python run_all.py --live
pause
