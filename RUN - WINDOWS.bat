@echo off
REM ---- Abacus Work Package Tracker launcher (Windows) ----
REM Double-click this file to start the tracker.
cd /d "%~dp0"

REM Find a Python launcher
set PY=
where py >nul 2>&1 && set PY=py
if not defined PY ( where python >nul 2>&1 && set PY=python )

if not defined PY (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add Python to PATH"^),
  echo then double-click this file again.
  pause
  exit /b 1
)

echo Starting Abacus Work Package Tracker...
echo Your browser will open automatically at http://127.0.0.1:5010
echo (Press Ctrl+C to stop)

%PY% -m pip install -r requirements.txt >nul 2>&1
%PY% app.py
pause
