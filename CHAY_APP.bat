@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   Sever Dub Studio Pro -- Dang Khoi Dong...
echo ========================================

set PYTHON=python

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :RUN

if exist "C:\Users\MaiThanhChu\AppData\Local\Programs\Python\Python311\python.exe" (
    set "PYTHON=C:\Users\MaiThanhChu\AppData\Local\Programs\Python\Python311\python.exe"
    goto :RUN
)

if exist "C:\Users\MaiThanhChu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" (
    set "PYTHON=C:\Users\MaiThanhChu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
    goto :RUN
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :RUN
)

echo [!] Khong tim thay Python. Vui long kiem tra lai PATH.
pause
exit /b 1

:RUN
"%PYTHON%" run.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Ung dung da dung voi ma loi %ERRORLEVEL%.
    pause
)
