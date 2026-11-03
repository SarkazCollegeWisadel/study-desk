@echo off
cd /d "%~dp0"
if not exist "..\.venv\Scripts\python.exe" (
    echo Create .venv and install requirements first. See README.md.
    pause
    exit /b 1
)
"..\.venv\Scripts\python.exe" build_kb.py serve --port 8765 --open
pause
