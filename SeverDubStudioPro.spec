# -*- mode: python ; coding: utf-8 -*-
import os
import rapidocr_onnxruntime
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

_TARGET_OS = os.environ.get("SEVER_BUILD_OS", "windows").lower()
if _TARGET_OS not in {"windows", "linux"}:
    raise SystemExit("SEVER_BUILD_OS must be windows or linux")
_IS_LINUX = _TARGET_OS == "linux"
if _IS_LINUX and os.name != "posix":
    raise SystemExit("Linux artifact must be built on Linux (native or WSL), not Windows")

def _collect_webview_hiddenimports():
    if _IS_LINUX:
        return ['webview', 'webview.platforms.gtk']
    return ['webview', 'webview.platforms.edgechromium', 'clr', 'pythonnet']

_WEBVIEW_HIDDENIMPORTS = _collect_webview_hiddenimports()


rapidocr_dir = os.path.dirname(rapidocr_onnxruntime.__file__)

# ── Settings: luôn bundle bản mẫu trung tính (settings runtime nằm ở %APPDATA%) ──
import shutil
os.makedirs('.build_settings', exist_ok=True)
_settings_src = os.path.join('.build_settings', 'settings.json')
shutil.copy('settings.example.json', _settings_src)

# ── Bundle STT models (Bản Lite: Tự tải về %APPDATA%/SeverDesktop/models_cache; Bản Full: bundle kèm) ──
_BUILD_LITE = os.environ.get("BUILD_LITE", "0") == "1"
_models_datas = []

if not _BUILD_LITE:
    _candidate_dirs = [
        os.path.join(os.environ['SEVER_DESKTOP_DIR'], 'models_cache', 'models') if os.environ.get('SEVER_DESKTOP_DIR') else '',
        os.path.join(os.environ.get('APPDATA', ''), 'SeverDesktop', 'models_cache', 'models'),
        os.path.join(os.path.expanduser('~'), '.config', 'sever-desktop', 'models_cache', 'models'),
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

# ── Binaries Data (Tùy chọn: nếu NO_BIN=1 thì app tự tải về app_dir/bin) ──
_NO_BIN = os.environ.get("NO_BIN", "0") == "1"
_bin_datas = []
_bin_binaries = []
if not _NO_BIN and not _IS_LINUX:
    for _b in ('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'bin/yt-dlp.exe', 'bin/BBDown.exe'):
        if os.path.exists(_b):
            _bin_datas.append((_b, 'bin'))
# Linux uses distro-managed ffmpeg/ffprobe and pip yt-dlp; setup-linux.sh installs them.
if _IS_LINUX:
    import shutil as _shutil
    _missing_linux_tools = [n for n in ('ffmpeg', 'ffprobe', 'yt-dlp') if not _shutil.which(n)]
    if _missing_linux_tools:
        raise SystemExit("Missing Linux tools: " + ", ".join(_missing_linux_tools) + ". Run setup-linux.sh")
    # FFmpeg and yt-dlp come from OS packages, not copied shebang launchers.
    if not _shutil.which('gobject-introspection') and not os.path.exists('/usr/lib/python3/dist-packages/gi'):
        raise SystemExit("PyGObject/GTK not found; run setup-linux.sh")


# ── Piper TTS Data & Local Voice Models ──
_piper_datas = collect_data_files('piper')
_voice_onnx = os.path.join('models', 'piper', 'ngoc_huyen', 'ngoc_huyen.onnx')
_voice_json = os.path.join('models', 'piper', 'ngoc_huyen', 'ngoc_huyen.onnx.json')
if os.path.exists(_voice_onnx) and os.path.exists(_voice_json):
    _piper_datas.append(('models/piper', 'models/piper'))
elif os.environ.get('BUILD_LITE', '0') != '1':
    raise SystemExit(
        "Thiếu models/piper/ngoc_huyen/ngoc_huyen.onnx(.json) — bản FULL phải nhúng voice train sẵn.\n"
        "Tải 2 file vào models/piper/ngoc_huyen/ rồi build lại."
    )

_piper_submodules = [s for s in collect_submodules('piper') if not s.startswith('piper.train')]

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=_bin_binaries,
    datas=[
        ('src', 'src'),
        ('assets', 'assets'),
        (_settings_src, '.'),
    ] + _bin_datas + _playwright_datas + _rapidocr_datas + _models_datas + _piper_datas + collect_data_files('funasr') + collect_data_files('modelscope'),
    hiddenimports=[
        *_WEBVIEW_HIDDENIMPORTS,
        *(['gi', 'gi.repository', 'gi.repository.Gtk', 'gi.repository.WebKit2'] if _IS_LINUX else []),
        *(['rubicon'] if _IS_LINUX else []),
        # uvicorn
        'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
        # pywebview
        # OCR
        'rapidocr_onnxruntime', 'onnxruntime',
        # FunASR + torch
        'funasr', 'modelscope', 'torch',
        # fbank backend (funasr mới không kèm torchaudio)
        'kaldi_native_fbank',
        # FastAPI form upload
        'python_multipart', 'multipart',
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
    console=_IS_LINUX,
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
