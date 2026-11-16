@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Chua co moi truong - dang cai dat lan dau...
    call setup.bat
    if not exist ".venv\Scripts\python.exe" exit /b 1
)

".venv\Scripts\python.exe" -c "import fastapi, multipart, piper, funasr, kaldi_native_fbank" >nul 2>&1
if errorlevel 1 (
    echo Thieu thu vien - dang cai lai...
    call setup.bat
)

echo ========================================
echo   Sever Dub Studio Pro -- Dang Khoi Dong...
echo ========================================
".venv\Scripts\python.exe" run.py
if errorlevel 1 (
    echo.
    echo [!] Ung dung da dung voi ma loi %ERRORLEVEL%.
    pause
)
