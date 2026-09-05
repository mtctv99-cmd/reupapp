"""PyInstaller runtime hook: fix FunASR in frozen apps.

funasr.register.RegisterTables.register calls inspect.getfile()/getsourcelines()
inside a class decorator. In a frozen (PyInstaller) app there is no source code,
so those calls raise OSError. Because funasr/__init__.py runs
`import_submodules(__name__)` at import time, ANY module whose decorator raises
fails to finish importing (e.g. funasr/tokenizer/char_tokenizer.py ends before
defining the module-level `load_seg_dict`, or the class never registers at all).

We monkey-patch the `inspect` functions BEFORE funasr is ever imported so the
decorator never raises. Normal behaviour is preserved when source is available.
"""
import inspect
import os
import sys
from pathlib import Path

# ── Global silence for Windows subprocesses (Chống triệt để cửa sổ đen nhấp nháy trên Windows) ──
if sys.platform == "win32":
    import subprocess
    if not getattr(subprocess, "_SEVER_SILENT_PATCHED", False):
        _orig_popen = subprocess.Popen

        class _SilentPopen(_orig_popen):
            def __init__(self, *args, **kwargs):
                if "creationflags" not in kwargs:
                    kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                else:
                    kwargs["creationflags"] |= 0x08000000
                super().__init__(*args, **kwargs)

        subprocess.Popen = _SilentPopen
        subprocess._SEVER_SILENT_PATCHED = True

    try:
        from asyncio import windows_utils
        if not getattr(windows_utils, "_SEVER_SILENT_PATCHED", False):
            _orig_win_popen_init = windows_utils.Popen.__init__

            def _silent_win_popen_init(self, *args, **kwargs):
                if "creationflags" not in kwargs:
                    kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                else:
                    kwargs["creationflags"] |= 0x08000000
                _orig_win_popen_init(self, *args, **kwargs)

            windows_utils.Popen.__init__ = _silent_win_popen_init
            windows_utils._SEVER_SILENT_PATCHED = True
    except Exception:
        pass

_IS_FROZEN = bool(getattr(sys, "frozen", False))

if _IS_FROZEN:
    # ── STT models resolution: 1. bundled models_cache -> 2. %APPDATA%/SeverDesktop/models_cache -> 3. user cache ──
    _bundle_root = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable))))
    _bundled_cache = _bundle_root / "models_cache"
    _app_data_cache = Path(os.environ.get("APPDATA", str(Path.home()))) / "SeverDesktop" / "models_cache"

    if _bundled_cache.is_dir():
        os.environ.setdefault("MODELSCOPE_CACHE", str(_bundled_cache))
        os.environ.setdefault("HUB_CACHE", str(_bundled_cache))
    elif _app_data_cache.is_dir():
        os.environ.setdefault("MODELSCOPE_CACHE", str(_app_data_cache))
        os.environ.setdefault("HUB_CACHE", str(_app_data_cache))

    _orig_getsource = inspect.getsource
    _orig_getfile = inspect.getfile
    _orig_getsourcelines = inspect.getsourcelines
    _orig_findsource = inspect.findsource

    def _safe_findsource(obj):
        try:
            return _orig_findsource(obj)
        except (OSError, TypeError):
            return (["<frozen>"], 0)

    def _safe_getsourcelines(obj):
        try:
            return _orig_getsourcelines(obj)
        except (OSError, TypeError):
            return (["<frozen>"], 0)

    def _safe_getfile(obj):
        try:
            return _orig_getfile(obj)
        except (OSError, TypeError):
            return "<frozen>"

    def _safe_getsource(obj):
        try:
            return _orig_getsource(obj)
        except (OSError, TypeError):
            return "<frozen source>"

    inspect.findsource = _safe_findsource
    inspect.getsourcelines = _safe_getsourcelines
    inspect.getfile = _safe_getfile
    inspect.getsource = _safe_getsource
