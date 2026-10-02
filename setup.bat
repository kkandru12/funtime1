@echo off
REM One-click environment setup. Double-click after extracting a new zip.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Creating venv...
    py -3.13 -m venv venv
)
.\venv\Scripts\python -m pip install --upgrade pip
.\venv\Scripts\python -m pip install -r requirements.txt
echo.
echo DONE. Now run, in three separate windows:
echo   venv\Scripts\python gex_bridge\main.py
echo   venv\Scripts\python algo\main.py
echo   venv\Scripts\python algo_es\main.py
pause
