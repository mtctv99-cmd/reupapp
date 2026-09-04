@echo off
title Sever Dub Studio Pro - Build Full Offline Installer
echo ========================================================
echo   Building Sever Dub Studio Pro (FULL OFFLINE BUILD)...
echo   Includes: STT AI Models (FunASR/Paraformer) + Binaries
echo ========================================================

set BUILD_LITE=0
set NO_BIN=0

pyinstaller SeverDubStudioPro.spec --clean --noconfirm

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] PyInstaller Build failed!
    pause
    exit /b 1
)

echo ========================================================
echo   Cleaning deeply nested licenses to prevent MAX_PATH...
echo ========================================================
python -c "import shutil, os; [shutil.rmtree(os.path.join(r, d), ignore_errors=True) for r, ds, fs in os.walk(r'dist\SeverDubStudioPro') for d in ds if 'licenses' in d.lower()]"

echo ========================================================
echo   Compiling Inno Setup (Setup_Full_Offline.exe)...
echo ========================================================
set "ISCC_PATH="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if "%ISCC_PATH%"=="" (
    where ISCC.exe >nul 2>&1
    if %ERRORLEVEL% EQU 0 set "ISCC_PATH=ISCC.exe"
)

if "%ISCC_PATH%"=="" (
    echo [ERROR] Khong tim thay ISCC.exe de dong goi file Setup.
    pause
    exit /b 1
)

"%ISCC_PATH%" /DMyAppVersion=0.2.0 /DOutputBaseFilename=SeverDubStudioPro_Full_Offline_Setup_v0.2.0 installer.iss

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Inno Setup Compilation failed!
    pause
    exit /b 1
)

echo ========================================================
echo   BUILD FULL OFFLINE SUCCESS!
echo   Setup File: dist\SeverDubStudioPro_Full_Offline_Setup_v0.2.0.exe
echo ========================================================
pause
