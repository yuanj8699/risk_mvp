@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv
    if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe -c "import fastapi, sqlalchemy, openpyxl, uvicorn, httpx, multipart" >nul 2>&1
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe run.py
goto :eof
:failed
echo Setup failed. Install Python 3.11 or newer and retry.
pause
