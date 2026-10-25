@echo off
setlocal
cd /d "%~dp0"
title VU CampusFix

set PY=python
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/windows/
    echo During setup, tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  set PY=py -3
)
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required. Please upgrade Python, then retry.
  pause
  exit /b 1
)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example.
)
echo.
echo Starting VU CampusFix. Keep this window open.
echo The moderator token is printed below - you need it to edit or delete reports.
echo.
%PY% app\campusfix_vu.py
pause
