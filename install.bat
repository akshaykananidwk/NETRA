@echo off
rem ============================================================
rem  KRISHNA NETRA - one-time installer
rem  Installs whatever is missing: Python 3.11, venv, packages.
rem  Run once; afterwards just use start.bat
rem ============================================================
chcp 65001 >nul
title Krishna Netra - Install
cd /d "%~dp0"

echo.
echo  ============================================
echo   KRISHNA NETRA  -  INSTALL
echo  ============================================
echo.

rem --- 1. find (or install) Python 3.11 ---
set "PY_CMD="
py -3.11 --version >nul 2>&1 && set "PY_CMD=py -3.11"
if not defined PY_CMD (
    python --version 2>nul | findstr /r "3\.1[123]" >nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    echo  [1/5] Python 3.11 not found - installing via winget...
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo  !! winget failed. Install Python 3.11 manually from:
        echo     https://www.python.org/downloads/release/python-3119/
        echo     ^(tick "Add python.exe to PATH"^) then run install.bat again.
        pause
        exit /b 1
    )
    rem refresh PATH for this session
    set "PATH=%LocalAppData%\Programs\Python\Python311;%LocalAppData%\Programs\Python\Python311\Scripts;%PATH%"
    set "PY_CMD=python"
)
echo  [1/5] Python OK:
%PY_CMD% --version

rem --- 2. virtual environment ---
if not exist ".venv\Scripts\python.exe" (
    echo  [2/5] Creating virtual environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 ( echo  !! venv creation failed & pause & exit /b 1 )
) else (
    echo  [2/5] Virtual environment already exists
)

rem --- 3. packages ---
echo  [3/5] Installing packages (first time takes a few minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  Some packages need a C++ compiler - retrying without those...
    findstr /v /i /c:"insightface" requirements.txt > "%TEMP%\kn_requirements.txt"
    ".venv\Scripts\python.exe" -m pip install -r "%TEMP%\kn_requirements.txt"
    if errorlevel 1 ( echo  !! install failed - check internet & pause & exit /b 1 )
)

rem --- 3b. face engine: use prebuilt wheel when source build is impossible ---
".venv\Scripts\python.exe" -c "import insightface" >nul 2>&1
if errorlevel 1 (
    echo  [3b]  Installing insightface prebuilt Windows wheel...
    ".venv\Scripts\python.exe" -m pip install "https://github.com/Gourieff/Assets/raw/main/Insightface/insightface-0.7.3-cp311-cp311-win_amd64.whl"
)
".venv\Scripts\python.exe" -c "import insightface" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  !! insightface could not be installed - face recognition stays OFF.
    echo     The app still runs; the AI badge will show red.
    echo     Permanent fix: open "Visual Studio Installer", add the
    echo     "Desktop development with C++" workload ^(includes Windows SDK^),
    echo     then run install.bat again.
    echo.
) else (
    echo  [3b]  Face engine OK
)

rem --- 3c. optional offline TTS - fine if this fails (no py3.11 wheel) ---
echo  [3c]  Trying optional piper-tts (ok if it fails)...
".venv\Scripts\python.exe" -m pip install piper-tts >nul 2>&1
if errorlevel 1 ( echo        piper-tts skipped - edge-tts will be the voice )

rem --- 4. config + folders ---
if not exist ".env" (
    echo  [4/5] Creating .env from .env.example
    copy /y ".env.example" ".env" >nul
) else (
    echo  [4/5] .env already exists - keeping it
)
if not exist "data"   mkdir data
if not exist "logs"   mkdir logs
if not exist "models" mkdir models

rem --- 5. firewall rule so the phone on Wi-Fi can open the UI ---
echo  [5/5] Adding firewall rule for port 8000 (needs admin - ok if it fails)
netsh advfirewall firewall add rule name="KrishnaNetra8000" dir=in action=allow protocol=TCP localport=8000 >nul 2>&1

echo.
echo  ============================================
echo   DONE!  Now double-click  start.bat
echo  ============================================
echo.
pause
