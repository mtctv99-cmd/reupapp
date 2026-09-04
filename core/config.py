"""App config manager — providers, preferences, job defaults."""
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

_CONFIG_DIR: Path | None = None


def get_bin_dir() -> Path | None:
    """Trả về thư mục bin (chứa ffmpeg, ffprobe, yt-dlp, BBDown).
    Ưu tiên _internal/bin hoặc bin/, fallback về %APPDATA%/SeverDesktop/bin.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent.parent
    for sub in ("bin", Path("_internal") / "bin"):
        p = base / sub
        if p.exists() and p.is_dir():
            return p
    # Fallback to APPDATA bin directory
    app_bin = app_dir() / "bin"
    if app_bin.exists() and app_bin.is_dir():
        return app_bin
    return None


def _bundled_bin(name: str) -> str | None:
    """Find bundled binary (ffmpeg, yt-dlp, etc.) in PyInstaller _internal/bin/ or project bin/."""
    bin_dir = get_bin_dir()
    if bin_dir:
        p = bin_dir / name
        if p.exists():
            return str(p)
        if os.name == "nt":
            p = bin_dir / f"{name}.exe"
            if p.exists():
                return str(p)
    return None


def ensure_env_paths() -> None:
    """Tự động thêm thư mục bin nhúng vào PATH và cấu hình pydub."""
    bin_dir = get_bin_dir()
    if bin_dir:
        bin_str = str(bin_dir.resolve())
        curr_path = os.environ.get("PATH", "")
        if bin_str not in curr_path:
            os.environ["PATH"] = bin_str + os.pathsep + curr_path

    # Cấu hình pydub nếu có
    try:
        from pydub import AudioSegment
        ffmpeg = _bundled_bin("ffmpeg") or "ffmpeg"
        ffprobe = _bundled_bin("ffprobe") or "ffprobe"
        AudioSegment.converter = ffmpeg
        AudioSegment.ffmpeg = ffmpeg
        AudioSegment.ffprobe = ffprobe
    except Exception:
        pass


def app_dir() -> Path:
    global _CONFIG_DIR
    if _CONFIG_DIR is None:
        env_val = os.environ.get("SEVER_DESKTOP_DIR", "").strip()
        if env_val:
            base = Path(env_val)
        else:
            system = platform.system()
            if system == "Windows":
                base = Path(os.environ.get("APPDATA", "")) / "SeverDesktop"
            elif system == "Darwin":
                base = Path.home() / "Library" / "Application Support" / "SeverDesktop"
            else:
                base = Path.home() / ".config" / "sever-desktop"
        base.mkdir(parents=True, exist_ok=True)
        _CONFIG_DIR = base
    return _CONFIG_DIR


def _get_settings_file_path() -> Path:
    """Khi frozen (EXE): Ưu tiên lưu settings.json ngay cạnh file EXE. Nếu không ghi được thì dùng %APPDATA%."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        local_cfg = exe_dir / "settings.json"
        if local_cfg.exists():
            return local_cfg
        try:
            # Test ghi file ở exe_dir
            test_file = exe_dir / ".perm_check"
            test_file.touch()
            test_file.unlink()
            return local_cfg
        except Exception:
            pass
    _proj = Path(__file__).parent.parent / "settings.json"
    return _proj if _proj.exists() else (app_dir() / "settings.json")


def _defaults() -> dict[str, Any]:
    return {
        "download_dir": str(app_dir() / "downloads"),
        "output_dir": str(app_dir() / "output"),
        "stt_provider": "funasr",
        "source_language": "zh",
        "target_language": "vi",
        "remove_sub": True,
        "video_speed": 1.0,
        "original_volume": 100,
        "aspect_ratio": "original",
        "bg_style": "blur",
        "bg_color": "black",
        "logo_path": "",
        "logo_position": "top_right",
        "logo_opacity": 0.8,
        "sub_font": "Arial",
        "sub_font_size": 38,
        "sub_style": "white_black",
        "stt_device": "cpu",
        "bbdown_path": _bundled_bin("BBDown") or "BBDown",
        "bbdown_cookie": "",
        "ytdlp_path": _bundled_bin("yt-dlp") or "yt-dlp",
        "ffmpeg_path": _bundled_bin("ffmpeg") or "ffmpeg",
        "ffprobe_path": _bundled_bin("ffprobe") or "ffprobe",
        # ── Clean real configs (No demo data) ──
        "translate_provider": {
            "url": "https://gemini.maika.sbs/v1",
            "api_key": "none",
            "models": ["gemini-flash-lite", "gemini-3.5-flash-lite"]
        },
        "translate_tiers": [
            {
                "provider": "gemini_stealth",
                "model": "gemini-flash-lite",
                "label": "Tier 1: gemini-flash-lite (local, free)"
            },
            {
                "provider": "openai",
                "url": "https://gemini.maika.sbs/v1",
                "model": "gemini-flash-lite",
                "label": "Tier 2: gemini-flash-lite (provider)"
            },
            {
                "provider": "openai",
                "url": "https://gemini.maika.sbs/v1",
                "model": "gemini-3.5-flash-lite",
                "label": "Tier 3: gemini-3.5-flash-lite (provider)"
            },
            {
                "provider": "google",
                "model": "google",
                "label": "Tier 4: Google Dịch"
            }
        ],
        "tts_provider": {
            "url": "https://tts.maika.sbs",
            "api_key": "omni-secret-key-123",
            "voices": ["ngoc_huyen", "ngoc_huyen"]
        },
        "tts_servers": [
            {
                "url": "https://tts.maika.sbs",
                "api_key": "omni-secret-key-123",
                "voice": "ngoc_huyen"
            }
        ],
    }


class Settings:
    def __init__(self) -> None:
        self._data: dict[str, Any] = _defaults()
        self._load()

    def _load(self) -> None:
        ensure_env_paths()
        p = _get_settings_file_path()
        merged = _defaults()
        if p.exists():
            try:
                loaded = json.loads(p.read_text("utf-8"))
                # ponytail: nếu file settings cũ trên máy khách bị thiếu translate_tiers thì lấy defaults
                if not loaded.get("translate_tiers"):
                    loaded["translate_tiers"] = _defaults()["translate_tiers"]
                merged.update(loaded)
            except Exception:
                pass

        # ponytail: Luôn ưu tiên tìm binary nhúng sẵn (_internal/bin) trước, tránh dán cứng đường dẫn tuyệt đối của máy dev
        for key in ("ffmpeg_path", "ffprobe_path", "ytdlp_path", "bbdown_path"):
            val = merged.get(key, "")
            bare = Path(val).stem.lower() if val else key.split("_")[0]
            bundled = _bundled_bin(bare)
            if bundled:
                merged[key] = bundled
            elif not val or not Path(val).exists():
                merged[key] = bare

        # Chuẩn hóa download_dir và output_dir: nếu rỗng hoặc trỏ tới thư mục không tồn tại của máy khác -> dùng app_dir()
        for d_key in ("download_dir", "output_dir"):
            d_val = merged.get(d_key, "")
            if not d_val or not os.path.exists(d_val):
                fallback_d = app_dir() / ("downloads" if d_key == "download_dir" else "output")
                fallback_d.mkdir(parents=True, exist_ok=True)
                merged[d_key] = str(fallback_d)

        self._data = merged

    def save(self) -> None:
        _get_settings_file_path().write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def get_tts_servers(self) -> list[dict[str, Any]]:
        return self._data.get("tts_servers", [])

    def set_tts_servers(self, servers: list[dict[str, Any]]) -> None:
        self._data["tts_servers"] = servers
        self.save()

    def get_translate_tiers(self) -> list[dict[str, Any]]:
        return self._data.get("translate_tiers", [])

    def set_translate_tiers(self, tiers: list[dict[str, Any]]) -> None:
        self._data["translate_tiers"] = tiers
        self.save()

    def get_stt_provider(self, name: str = "") -> dict[str, Any]:
        return self._data.get(f"stt_{name}", {}) or self._data.get("stt_config", {})

    @property
    def data(self) -> dict[str, Any]:
        return self._data


settings = Settings()
