@echo off
title Sever Dub Studio Pro - Build Bản Lite (~300MB)
echo ========================================
echo   Building SeverDubStudioPro Lite EXE...
echo   (Tach Model AI de file sieu nhe ~300MB)
echo ========================================

set BUILD_LITE=1
pyinstaller SeverDubStudioPro.spec --clean --noconfirm

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
