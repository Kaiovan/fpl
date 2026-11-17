@echo off
rem Double-click this file to start the FPL Assistant. Close the window (or press Ctrl+C) to stop it.
title FPL Assistant
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The .venv folder is missing. Run these two commands in this folder once, then try again:
  echo.
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" server.py

echo.
echo The server has stopped. If you saw an error about the address or port being in use,
echo another copy is already running: close that window first, then run this again.
pause
