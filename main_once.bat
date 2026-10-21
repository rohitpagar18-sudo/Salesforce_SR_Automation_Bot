@echo off
cd /d "%~dp0"
echo Project folder: %CD%

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Creating it...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
)

echo Checking required packages...
".venv\Scripts\python.exe" -c "import yaml, httpx, dotenv" 2>nul
if errorlevel 1 (
    echo Installing required packages...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Package installation failed.
        pause
        exit /b 1
    )
)

set "PYTHONPATH=%CD%\src"

echo Starting SR Automation...
echo.

".venv\Scripts\python.exe" -m sr_automation.main --config config\uat.yaml

echo.
echo Automation cycle completed.

pause