@echo off
cd /d "%~dp0"
title ClipForge update
echo Updating the YouTube downloader and other packages...
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
echo.
echo Done. Restart ClipForge (close its window, then double-click start.bat).
pause
