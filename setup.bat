@echo off
setlocal
cd /d "%~dp0"
title ClipForge setup
echo.
echo  ===============================================
echo   ClipForge setup
echo  ===============================================
echo.

REM ---------- 1. Python ----------
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,13)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [1/5] Python 3.10-3.13 not found. Installing Python 3.12...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo  Python was installed. Close this window and double-click setup.bat again.
  pause
  exit /b 1
)
echo [1/5] Using Python: %PY%

REM ---------- 2. Python packages ----------
if not exist ".venv\Scripts\python.exe" (
  echo [2/5] Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
echo [2/5] Installing Python packages (this takes a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail

REM ---------- 3. ffmpeg ----------
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [3/5] Installing ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
) else (
  echo [3/5] ffmpeg found.
)

REM ---------- 4. Deno (needed by the YouTube downloader) ----------
where deno >nul 2>&1
if errorlevel 1 (
  if not exist "%USERPROFILE%\.deno\bin\deno.exe" (
    echo [4/5] Installing Deno for YouTube downloads...
    winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
  ) else (
    echo [4/5] Deno found.
  )
) else (
  echo [4/5] Deno found.
)

REM ---------- 5. Claude Code ----------
where claude >nul 2>&1
if errorlevel 1 (
  if not exist "%USERPROFILE%\.local\bin\claude.exe" (
    echo [5/5] Installing Claude Code...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
  ) else (
    echo [5/5] Claude Code found.
  )
) else (
  echo [5/5] Claude Code found.
)

echo.
echo  ===============================================
echo   Setup finished.
echo  ===============================================
echo.
echo   One more thing if you have never used Claude Code:
echo     open a NEW terminal, type   claude   and log in
echo     with your Claude account. Then type /exit.
echo.
echo   Then double-click start.bat
echo.
pause
exit /b 0

:fail
echo.
echo  Setup failed. Scroll up to see the error.
pause
exit /b 1
