@echo off
title Sever Dub Studio Pro - Build Bản Lite (~300MB)
echo ========================================
echo   Building SeverDubStudioPro Lite EXE...
echo   (Tach Model AI de file sieu nhe ~300MB)
echo ========================================

set BUILD_LITE=1
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Chua co .venv. Chay setup.bat truoc.
    pause
    exit /b 1
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 "%PY%" -m pip install pyinstaller

"%PY%" -m PyInstaller SeverDubStudioPro.spec --clean --noconfirm

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo ========================================
echo   Build Lite SUCCESS!
echo   Output: dist\SeverDubStudioPro\
echo ========================================
pause
