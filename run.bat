@echo off
cd /d "%~dp0"
.venv\Scripts\python glamcam.py %*
if errorlevel 1 pause
