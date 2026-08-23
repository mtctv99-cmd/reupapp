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

_IS_FROZEN = bool(getattr(sys, "frozen", False))

if _IS_FROZEN:
    # ── Offline STT models: point modelscope cache to the bundled models ──
    _bundle_root = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable))))
    _bundled_cache = _bundle_root / "models_cache"
    if _bundled_cache.is_dir():
        os.environ.setdefault("MODELSCOPE_CACHE", str(_bundled_cache))
        os.environ.setdefault("HUB_CACHE", str(_bundled_cache))
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
            try:
                return inspect.getsourcefile(obj) or "<frozen>"
            except Exception:
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
