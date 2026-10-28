@echo off
rem Builds dist\GlamCam\GlamCam.exe (Windows). Copy the whole dist\GlamCam folder wherever you like.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.bat --no-pause || goto :fail
.venv\Scripts\python -m pip install -q pyinstaller || goto :fail
.venv\Scripts\python glamcam.py --download-models || goto :fail
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
.venv\Scripts\python -m PyInstaller --noconfirm --log-level WARN glamcam.spec || goto :fail
start /wait "" dist\GlamCam\GlamCam.exe --self-test || goto :fail
echo.
echo Built dist\GlamCam\GlamCam.exe  (keep it inside its folder; make a shortcut to it)
pause
exit /b 0

:fail
echo.
echo Build failed. Scroll up for the error.
pause
exit /b 1
