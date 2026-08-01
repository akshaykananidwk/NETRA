@echo off
rem ============================================================
rem  KRISHNA NETRA - one-click start
rem  - installs first if needed
rem  - starts the server with an auto-restart loop
rem    (a GitHub update restarts through this loop automatically)
rem  - opens Google Chrome on http://localhost:8000
rem  Close this window (or Ctrl+C) to stop the server.
rem ============================================================
chcp 65001 >nul
title Krishna Netra
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run - installing...
    call install.bat
    if not exist ".venv\Scripts\python.exe" exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul

rem if an update delivered new batch files, adopt them on next start
if exist "install.bat.new" move /y "install.bat.new" "install.bat" >nul

rem open Chrome once the server answers (hidden helper, waits up to 60 s)
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
 "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing 'http://localhost:8000/api/system/status' -TimeoutSec 2|Out-Null; try{Start-Process chrome 'http://localhost:8000'}catch{Start-Process 'http://localhost:8000'}; break}catch{Start-Sleep 1}}"

:loop
echo.
echo  [%date% %time%]  Krishna Netra starting...
".venv\Scripts\python.exe" main.py
echo.
echo  Server stopped (update/restart). Starting again in 2 seconds...
echo  (Close this window to stop completely)
if exist "start.bat.new" (
    move /y "start.bat.new" "start.bat" >nul
    start "" "%~f0"
    exit
)
timeout /t 2 /nobreak >nul
goto loop
