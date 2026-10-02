@echo off
REM update.bat — pull latest code from GitHub, reinstall if needed, done.
cd /d "%~dp0"
echo Pulling latest code...
git pull origin main
if errorlevel 1 (
    echo Git pull failed. Make sure you're in a git repo linked to the repo.
    pause
    exit /b 1
)
echo.
echo Updating dependencies...
venv\Scripts\python -m pip install -r requirements.txt --quiet
echo.
echo DONE. Run start.bat to launch.
pause
