@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install it with Add Python to PATH enabled.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :error
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Freshhead: open http://127.0.0.1:8766 in your browser.
echo Keep this window open. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m freshhead
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo Startup failed. The error is printed above.
pause
exit /b 1
