@echo off
cd /d "%~dp0"
title ClipForge
if not exist ".venv\Scripts\python.exe" (
  echo ClipForge is not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
pause
