@echo off
rem ============================================================
rem  KRISHNA NETRA - package repair
rem  Fixes broken/partially-installed Python packages
rem  (e.g. "circular import" / "cannot import name" errors).
rem  CLOSE the start.bat window BEFORE running this!
rem ============================================================
chcp 65001 >nul
title Krishna Netra - Repair
cd /d "%~dp0"

echo.
echo  ============================================
echo   KRISHNA NETRA  -  PACKAGE REPAIR
echo  ============================================
echo.
echo  [1/3] Reinstalling commonly-corrupted libraries...
".venv\Scripts\python.exe" -m pip install --force-reinstall --no-cache-dir huggingface_hub hf_xet
echo.
echo  [2/3] Verifying all requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo  [3/3] Quick import check...
".venv\Scripts\python.exe" -c "import huggingface_hub; import faster_whisper; print('  faster-whisper OK'); import insightface; print('  insightface OK')"
if errorlevel 1 (
    echo.
    echo  !! Still broken. Deleting and reinstalling the whole venv usually
    echo     fixes it:  rmdir /s /q .venv   then run install.bat again.
) else (
    echo.
    echo  ============================================
    echo   REPAIRED!  Now run start.bat
    echo  ============================================
)
echo.
pause
