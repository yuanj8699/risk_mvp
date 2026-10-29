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
node -e "const v=process.versions.node.split('.').map(Number);process.exit(v[0]>22||(v[0]===22&&v[1]>=19)?0:1)" >nul 2>&1
if errorlevel 1 (
    echo Pi Agent requires Node.js 22.19 or newer.
    goto :failed
)
if not exist agent\node_modules\@earendil-works\pi-agent-core\package.json (
    call npm --prefix agent ci --ignore-scripts
    if errorlevel 1 goto :failed
)
if not exist agent\dist\runner.js (
    call npm --prefix agent run build
    if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe run.py
goto :eof
:failed
echo Setup failed. Check the error above. Python 3.11+ and Node.js 22.19+ are required.
pause
