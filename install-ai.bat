@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-vision.txt
if errorlevel 1 goto fail
echo.
echo AI dependencies installed. Start Freshhead and open the AI tab.
echo The first image analysis downloads FashionCLIP weights.
echo For an NVIDIA CUDA build see docs/AI.md. No API key is required.
pause
exit /b 0
:fail
echo Installation failed. Check the error above. Python 3.11+ is required.
pause
exit /b 1
