# -*- mode: python ; coding: utf-8 -*-
import os
import rapidocr_onnxruntime
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

rapidocr_dir = os.path.dirname(rapidocr_onnxruntime.__file__)

# ── Bundle STT models (offline-first: máy khác mở lên là dùng được luôn) ──
_MODELS_CACHE = os.path.join(os.path.expanduser('~'), '.cache', 'modelscope', 'models')
_models_datas = []
if os.path.exists(_MODELS_CACHE):
    for _d in sorted(os.listdir(_MODELS_CACHE)):
        _p = os.path.join(_MODELS_CACHE, _d)
        if os.path.isdir(_p):
            _models_datas.append((_p, os.path.join('models_cache', 'models', _d)))

# ── Dynamic Playwright Driver + Chromium Browser Detection ──
_playwright_datas = []
try:
    import playwright
    from playwright.sync_api import sync_playwright
    playwright_dir = os.path.dirname(playwright.__file__)
    pw_driver = os.path.join(playwright_dir, 'driver')
    if os.path.exists(pw_driver):
        _playwright_datas.append((pw_driver, 'playwright/driver'))

    # Chỉ lấy đúng 1 bản Chromium active mà Playwright đang dùng
    with sync_playwright() as p:
        pw_exec = p.chromium.executable_path
        if pw_exec and os.path.exists(pw_exec):
            p_curr = os.path.abspath(pw_exec)
            while os.path.basename(os.path.dirname(p_curr)) != 'ms-playwright' and os.path.dirname(p_curr) != p_curr:
                p_curr = os.path.dirname(p_curr)
            if os.path.exists(p_curr):
                _playwright_datas.append((p_curr, os.path.join('_ms-playwright', os.path.basename(p_curr))))
except Exception:
    pass

# ── RapidOCR Data ──
_rapidocr_datas = []
if os.path.exists(os.path.join(rapidocr_dir, 'config.yaml')):
    _rapidocr_datas.append((os.path.join(rapidocr_dir, 'config.yaml'), 'rapidocr_onnxruntime'))
if os.path.exists(os.path.join(rapidocr_dir, 'models')):
    _rapidocr_datas.append((os.path.join(rapidocr_dir, 'models'), 'rapidocr_onnxruntime/models'))

# ── Binaries Data ──
_bin_datas = []
for _b in ('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'bin/yt-dlp.exe', 'bin/BBDown.exe'):
    if os.path.exists(_b):
        _bin_datas.append((_b, 'bin'))

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src', 'src'),
        ('settings.json', '.'),
    ] + _bin_datas + _playwright_datas + _rapidocr_datas + _models_datas + collect_data_files('funasr') + collect_data_files('modelscope'),
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
        'gtts', 'gtts.tts',
        # TikTok TTS login
        'playwright', 'playwright.sync_api', 'playwright.driver',
        'playwright._impl', 'playwright.async_api',
        # HTTP
        'httpx', 'httpcore', 'h11',
        # Crypto & Web (yt-dlp)
        'Crypto', 'Cryptodome', 'websockets',
        # SQLite
        'sqlite3',
    ] + collect_submodules('funasr') + collect_submodules('modelscope'),
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
