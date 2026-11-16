@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Sever Dub Studio Pro - Cai dat moi truong
echo ========================================================

if exist ".venv\Scripts\python.exe" (
    echo [1/3] Da co .venv, bo qua tao moi.
    goto INSTALL
)

echo [1/3] Tim Python 3.10 - 3.12...
set "PY="
for %%V in (3.12 3.11 3.10) do if not defined PY (py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V")
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [!] Khong tim thay Python. Tai tai: https://www.python.org/downloads/
    echo     Nho tick "Add python.exe to PATH" khi cai.
    pause
    exit /b 1
)

%PY% -m venv .venv
if errorlevel 1 (
    echo [!] Tao venv that bai.
    pause
    exit /b 1
)

:INSTALL
echo [2/3] Cai thu vien (lan dau co the mat 5-15 phut)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Cai thu vien that bai. Kiem tra mang roi chay lai setup.bat
    pause
    exit /b 1
)

echo [3/3] Kiem tra thu vien...
".venv\Scripts\python.exe" -c "import fastapi, multipart, uvicorn, httpx, webview, pydub, piper, onnxruntime, funasr, kaldi_native_fbank, modelscope, rapidocr_onnxruntime, cv2; print('OK')"
if errorlevel 1 (
    echo [!] Thieu thu vien. Chay lai setup.bat
    pause
    exit /b 1
)

rem Playwright can dung Edge/Chrome co san cua Windows. Chi cai chromium khi may khong co.
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "CHROME2=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%EDGE%" if not exist "%CHROME%" if not exist "%CHROME2%" (
    echo [*] Khong thay Edge/Chrome - dang cai trinh duyet cho Playwright...
    ".venv\Scripts\python.exe" -m playwright install chromium
)


echo.
echo Da cai xong. Chay run.bat de mo ung dung.
pause
