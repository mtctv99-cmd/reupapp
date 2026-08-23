@echo off
title Sever Dub Studio Pro - Build EXE
echo ========================================
echo   Building SeverDubStudioPro.exe...
echo ========================================

pyinstaller SeverDubStudioPro.spec --clean --noconfirm

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo ========================================
echo   Build SUCCESS!
echo   Output: dist\SeverDubStudioPro\
echo ========================================
explorer dist\SeverDubStudioPro
pause
