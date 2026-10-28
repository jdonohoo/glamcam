@echo off
rem GlamCam one-time setup for Windows: makes .venv, installs deps, fetches models.
setlocal
cd /d "%~dp0"
set "PAUSE=pause"
if /i "%~1"=="--no-pause" set "PAUSE="

rem Needs Python 3.10-3.13; prefer 3.12. Uses the "py" launcher from python.org / winget installs.
set "PY="
for %%v in (3.12 3.11 3.13 3.10) do (
  if not defined PY (
    py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo GlamCam needs Python 3.10-3.13.
  echo Install it with:  winget install -e --id Python.Python.3.12
  echo then open a NEW terminal and run setup.bat again.
  %PAUSE%
  exit /b 1
)
echo Using %PY%

if exist .venv rmdir /s /q .venv
%PY% -m venv .venv || goto :fail
.venv\Scripts\python -m pip install -q --upgrade pip || goto :fail
.venv\Scripts\python -m pip install -q -r requirements.txt || goto :fail
.venv\Scripts\python glamcam.py --download-models || goto :fail
echo.
echo Setup done. Get glam with:  run.bat
%PAUSE%
exit /b 0

:fail
echo.
echo Setup failed. Scroll up for the error.
%PAUSE%
exit /b 1
