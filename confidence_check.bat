@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)
set "PYTHONPATH=%CD%\src"
echo Starting Salesforce SR Automation Confidence Check UI...
python "scripts\confidence_check.py"
if errorlevel 1 (
    echo.
    echo Confidence Check UI failed to start.
    pause
    exit /b 1
)
endlocal
