@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting up GlamCam ^(one time only^)...
  call setup.bat --no-pause || exit /b 1
)
.venv\Scripts\python glamcam.py %*
if errorlevel 1 pause
