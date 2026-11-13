@echo off
setlocal
title Task Sniper - Server
cd /d "%~dp0"

set "URL=http://127.0.0.1:8787/"
set "PY=%~dp0.venv\Scripts\python.exe"
set "MARK=%TEMP%\materials_sniper_browser.lock"

rem ---------- 1) port already listening: do not start again ----------
netstat -ano | findstr /R /C:":8787 .*LISTENING" >nul 2>&1
if not errorlevel 1 goto open_browser

rem wait a moment and check again, to avoid two simultaneous double-clicks
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr /R /C:":8787 .*LISTENING" >nul 2>&1
if not errorlevel 1 goto open_browser

rem ---------- 2) check venv ----------
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo.
    echo Please run these commands in this folder first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "fastapi>=0.110" "uvicorn[standard]" "pydantic>=2" "sse-starlette" playwright httpx
    echo.
    pause
    exit /b 1
)

echo ==========================================
echo            Task Sniper
echo   URL: %URL%
echo   Close this window to stop the server
echo ==========================================
echo.

rem ---------- 3) start server, open browser once it is ready ----------
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing '%URL%api/status' -TimeoutSec 1 | Out-Null; break}catch{Start-Sleep -Milliseconds 500}}; Start-Process '%URL%'; Set-Content -Path '%MARK%' -Value (Get-Date) -Encoding ASCII"

"%PY%" run.py
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [WARN] Server exited with code %RC%
    echo Another window may already be running the service. Close this window.
) else (
    echo [Server stopped]
)
pause
exit /b 0

rem ---------- already running: skip duplicate tab within 30 seconds ----------
:open_browser
powershell -NoProfile -ExecutionPolicy Bypass -Command "$m='%MARK%'; $open=$true; if(Test-Path $m){ if(((Get-Date)-(Get-Item $m).LastWriteTime).TotalSeconds -lt 30){ $open=$false } }; if($open){ Start-Process '%URL%'; Set-Content -Path $m -Value (Get-Date) -Encoding ASCII; Write-Host '[INFO] Browser opened.' } else { Write-Host '[INFO] Browser already opened just now, skipped.' }"
ping -n 2 127.0.0.1 >nul
exit /b 0
