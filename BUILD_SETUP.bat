@echo off
title Sever Dub Studio Pro - Build Setup Installer (.exe)
echo ===================================================
echo   Tao Trinh Cai Dat Setup.exe cho SeverDubStudioPro
echo ===================================================

:: 1. Kiem tra xem dist\SeverDubStudioPro da duoc build chua
if not exist "dist\SeverDubStudioPro\SeverDubStudioPro.exe" (
    echo [1/3] Dang build Ban Lite EXE qua PyInstaller...
    set BUILD_LITE=1
    pyinstaller SeverDubStudioPro.spec --clean --noconfirm
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Build PyInstaller that bai!
        pause
        exit /b 1
    )
) else (
    echo [1/3] Da tim thay dist\SeverDubStudioPro. Bo qua buoc build PyInstaller.
)

:: 2. Tim trinh bien dich Inno Setup (ISCC.exe)
echo [2/3] Tim trinh bien dich Inno Setup (ISCC.exe)...
set "ISCC_PATH="

if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if "%ISCC_PATH%"=="" (
    where ISCC.exe >nul 2>&1
    if %ERRORLEVEL% EQU 0 set "ISCC_PATH=ISCC.exe"
)

:: Neu may chua co Inno Setup, thong bao hoac tu dong cai dat qua winget neu co
if "%ISCC_PATH%"=="" (
    echo.
    echo [!] Chua tim thay Inno Setup 6 tren may.
    echo Dang thu cai dat Inno Setup qua winget...
    winget install --id JR.InnoSetup -e --silent --accept-source-agreements --accept-package-agreements
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)

if "%ISCC_PATH%"=="" (
    echo.
    echo [ERROR] Khong tim thay ISCC.exe de dong goi file Setup.
    echo Vui long cai dat Inno Setup 6 tu: https://jrsoftware.org/isdl.php
    echo Sau do chay lai file BUILD_SETUP.bat nay.
    pause
    exit /b 1
)

:: 3. Tien hanh dong goi file Setup .exe
echo [3/3] Dang dong goi bang Inno Setup: "%ISCC_PATH%"...
"%ISCC_PATH%" installer.iss

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Dong goi Setup that bai!
    pause
    exit /b 1
)

echo.
echo ===================================================
echo   DONG GOI THANH CONG FILE CAI DAT SETUP!
echo   File: dist\SeverDubStudioPro_Setup_v0.2.0.exe
echo ===================================================
explorer dist
pause
