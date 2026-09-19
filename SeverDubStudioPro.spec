# -*- mode: python ; coding: utf-8 -*-
import os
import rapidocr_onnxruntime
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

rapidocr_dir = os.path.dirname(rapidocr_onnxruntime.__file__)

# ── Bundle STT models (Bản Lite: Tự tải về %APPDATA%/SeverDesktop/models_cache; Bản Full: bundle kèm) ──
_BUILD_LITE = os.environ.get("BUILD_LITE", "0") == "1"
_models_datas = []

if not _BUILD_LITE:
    _candidate_dirs = [
        os.path.join(os.environ.get('APPDATA', ''), 'SeverDesktop', 'models_cache', 'models'),
        os.path.join(os.path.expanduser('~'), '.cache', 'modelscope', 'models'),
        os.path.join(os.path.expanduser('~'), '.cache', 'modelscope', 'hub', 'models'),
        os.path.join(os.path.expanduser('~'), '.cache', 'modelscope', 'hub'),
    ]
    for _base in _candidate_dirs:
        if os.path.exists(_base):
            for _root, _dirs, _files in os.walk(_base):
                for _d in _dirs:
                    if any(m in _d for m in ('speech_fsmn_vad', 'punc_ct-transformer', 'speech_seaco_paraformer')):
                        _p = os.path.join(_root, _d)
                        _models_datas.append((_p, os.path.join('models_cache', 'models', _d)))
            if _models_datas:
                break

# ── Dynamic Playwright Driver (Bỏ Chromium 412MB - Dùng msedge/chrome hệ thống) ──
_playwright_datas = []
try:
    import playwright
    playwright_dir = os.path.dirname(playwright.__file__)
    pw_driver = os.path.join(playwright_dir, 'driver')
    if os.path.exists(pw_driver):
        _playwright_datas.append((pw_driver, 'playwright/driver'))
except Exception:
    pass

# ── RapidOCR Data ──
_rapidocr_datas = []
if os.path.exists(os.path.join(rapidocr_dir, 'config.yaml')):
    _rapidocr_datas.append((os.path.join(rapidocr_dir, 'config.yaml'), 'rapidocr_onnxruntime'))
if os.path.exists(os.path.join(rapidocr_dir, 'models')):
    _rapidocr_datas.append((os.path.join(rapidocr_dir, 'models'), 'rapidocr_onnxruntime/models'))

# ── Binaries Data (Tùy chọn: nếu NO_BIN=1 thì app tự tải về %APPDATA%/SeverDesktop/bin) ──
_NO_BIN = os.environ.get("NO_BIN", "0") == "1"
_bin_datas = []
if not _NO_BIN:
    for _b in ('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'bin/yt-dlp.exe', 'bin/BBDown.exe'):
        if os.path.exists(_b):
            _bin_datas.append((_b, 'bin'))

# ── Piper TTS Data & Local Voice Models ──
_piper_datas = collect_data_files('piper')
if os.path.exists('models/piper'):
    _piper_datas.append(('models/piper', 'models/piper'))

_piper_submodules = [s for s in collect_submodules('piper') if not s.startswith('piper.train')]

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src', 'src'),
        ('settings.json', '.'),
    ] + _bin_datas + _playwright_datas + _rapidocr_datas + _models_datas + _piper_datas + collect_data_files('funasr') + collect_data_files('modelscope'),
    hiddenimports=[
        # uvicorn
        'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
        # pywebview
        'webview', 'webview.platforms.edgechromium', 'clr', 'pythonnet',
        # OCR
        'rapidocr_onnxruntime', 'onnxruntime',
        # FunASR + torch
        'funasr', 'modelscope', 'torch',
        # Audio processing
        'pydub', 'pydub.silence',
        # TTS
        'gtts', 'gtts.tts', 'piper', 'piper.voice', 'piper.espeakbridge',
        # TikTok TTS login
        'playwright', 'playwright.sync_api', 'playwright.driver',
        'playwright._impl', 'playwright.async_api',
        # HTTP
        'httpx', 'httpcore', 'h11',
        # Crypto & Web (yt-dlp)
        'Crypto', 'Cryptodome', 'websockets',
        # SQLite
        'sqlite3',
    ] + _piper_submodules + collect_submodules('funasr') + collect_submodules('modelscope'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hooks/fix_frozen_funasr.py'],
    excludes=['tkinter', 'matplotlib', 'pytest', 'PyQt5', 'PyQt6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='SeverDubStudioPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='SeverDubStudioPro',
)
