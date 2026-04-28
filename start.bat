@echo off
setlocal
cd /d "%~dp0"

if not exist server.py (
  echo server.py was not found beside this script.
  echo Please run the launcher from E:\project\cut, or create a shortcut to E:\project\cut\start.bat.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3.9+ and make sure python is in PATH.
  pause
  exit /b 1
)

if not exist outputs mkdir outputs
if not exist logs mkdir logs
if not exist cache mkdir cache
if not exist cache\transcripts mkdir cache\transcripts

echo.
echo Live clip system is starting...
echo Project root: %cd%
echo Logs: %cd%\logs
echo The browser will open automatically. If port 8787 is busy, watch this window for the actual URL.
echo Press Ctrl+C to stop
echo.

python server.py
pause
