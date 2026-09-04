"""AI Models & Binaries manager — kiểm tra và tự tải ModelScope STT models và Binaries (ffmpeg, ffprobe, yt-dlp, BBDown)."""
import asyncio
import logging
import os
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Any

from core.config import app_dir

logger = logging.getLogger(__name__)

MODELS_LIST = [
    {
        "id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "name": "FSMN VAD (Nhận diện khoảng lặng)",
        "folder": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "approx_mb": 5,
    },
    {
        "id": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
        "name": "CT-Transformer (Dấu câu phụ đề)",
        "folder": "punc_ct-transformer_cn-en-common-vocab471067-large",
        "approx_mb": 1100,
    },
    {
        "id": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "name": "Seaco-Paraformer ASR (Nhận dạng giọng nói)",
        "folder": "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "approx_mb": 950,
    },
]

BINARIES_CONFIG = [
    {
        "name": "yt-dlp.exe",
        "url": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
        "approx_mb": 18,
    },
    {
        "name": "BBDown.exe",
        "url": "https://github.com/nilaoda/BBDown/releases/download/1.6.3/BBDown_1.6.3_20240901_win-x64.zip",
        "is_zip": True,
        "extract_file": "BBDown.exe",
        "approx_mb": 18,
    },
    {
        "name": "ffmpeg.exe",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "is_zip": True,
        "extract_file": "bin/ffmpeg.exe",
        "approx_mb": 100,
    },
    {
        "name": "ffprobe.exe",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "is_zip": True,
        "extract_file": "bin/ffprobe.exe",
        "approx_mb": 100,
    },
]

DOWNLOAD_STATE = {
    "is_downloading": False,
    "current_model": "",
    "current_index": 0,
    "total_models": len(MODELS_LIST),
    "progress": 0.0,
    "error": "",
    "downloaded_mb": 0,
    "total_mb": 2055,
}

_DOWNLOAD_LOCK = threading.Lock()


def get_app_bin_dir() -> Path:
    """Thư mục bin lưu tại %APPDATA%/SeverDesktop/bin."""
    p = app_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def check_binaries_installed() -> dict[str, Any]:
    """Kiểm tra xem ffmpeg, ffprobe, yt-dlp, BBDown đã có đủ chưa."""
    from core.config import get_bin_dir
    bin_dir = get_bin_dir()
    required = ["ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe", "BBDown.exe"]
    missing = []
    
    if bin_dir and bin_dir.exists():
        for req in required:
            if not (bin_dir / req).exists() and not (bin_dir / req.replace(".exe", "")).exists():
                missing.append(req)
    else:
        missing = required

    return {
        "ready": len(missing) == 0,
        "missing": missing,
        "bin_dir": str(bin_dir) if bin_dir else str(get_app_bin_dir()),
    }


def download_missing_binaries(progress_cb: Callable[[str], None] | None = None) -> None:
    """Tự động tải các binary còn thiếu về %APPDATA%/SeverDesktop/bin."""
    target_dir = get_app_bin_dir()
    ctx = urllib.request.ssl._create_unverified_context()
    
    for item in BINARIES_CONFIG:
        dest_file = target_dir / item["name"]
        if dest_file.exists() and dest_file.stat().st_size > 1000:
            continue
        
        if progress_cb:
            progress_cb(f"Đang tải {item['name']}...")
        logger.info("Downloading binary %s from %s...", item["name"], item["url"])
        
        tmp_dest = target_dir / f"temp_{item['name']}"
        if item.get("is_zip"):
            tmp_zip = target_dir / "temp_bin.zip"
            urllib.request.urlretrieve(item["url"], str(tmp_zip))
            with zipfile.ZipFile(tmp_zip, "r") as z:
                # Tìm file trong zip
                for zinfo in z.infolist():
                    if zinfo.filename.endswith(item["name"]) or zinfo.filename.endswith(item.get("extract_file", "")):
                        with z.open(zinfo) as src, open(dest_file, "wb") as dst:
                            dst.write(src.read())
                        break
            if tmp_zip.exists():
                tmp_zip.unlink()
        else:
            urllib.request.urlretrieve(item["url"], str(dest_file))
            
    logger.info("All missing binaries verified in %s", target_dir)


def get_models_cache_dir() -> Path:
    """Trả về thư mục chứa cache model của app.
    Khi chạy đóng gói (Frozen): Ưu tiên thư mục models_cache nhúng sẵn trong bundle (_MEIPASS/models_cache).
    Khi phát triển: Trỏ về %APPDATA%/SeverDesktop/models_cache.
    """
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        bundled = meipass / "models_cache"
        if bundled.exists():
            return bundled
    p = app_dir() / "models_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_modelscope_models_dir() -> Path:
    """Thư mục chứa model cụ thể theo chuẩn ModelScope."""
    base = get_models_cache_dir()
    d = base / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_models_installed() -> dict[str, Any]:
    """Kiểm tra xem toàn bộ 3 model STT đã sẵn sàng chưa."""
    # 1. Kiểm tra trong bundle (nếu là bản Full)
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        bundled = meipass / "models_cache" / "models"
        if bundled.exists():
            all_exist = True
            for m in MODELS_LIST:
                f_name = m["folder"]
                b_candidates = [
                    bundled / f_name,
                    bundled / "iic" / f_name,
                    bundled / f"iic--{f_name}",
                ]
                if not any(c.exists() and any(c.iterdir()) for c in b_candidates):
                    all_exist = False
                    break
            if all_exist:
                return {"ready": True, "location": "bundled", "models": MODELS_LIST}

    # 2. Kiểm tra trong %APPDATA%/SeverDesktop/models_cache
    app_cache = get_models_cache_dir()
    app_models = get_modelscope_models_dir()
    all_exist = True
    missing = []
    for m in MODELS_LIST:
        f_name = m["folder"]
        candidates = [
            app_models / f_name,
            app_models / "iic" / f_name,
            app_models / f"iic--{f_name}",
            app_cache / "hub" / "models" / f_name,
            app_cache / "hub" / "models" / "iic" / f_name,
            app_cache / "hub" / "models" / f"iic--{f_name}",
            app_cache / "models" / f_name,
            app_cache / "models" / "iic" / f_name,
            app_cache / "models" / f"iic--{f_name}",
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / f_name,
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / "iic" / f_name,
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / f"iic--{f_name}",
            Path.home() / ".cache" / "modelscope" / "hub" / f_name,
        ]
        if any(c.exists() and any(c.iterdir()) for c in candidates):
            continue

        all_exist = False
        missing.append(m)

    return {
        "ready": all_exist,
        "location": str(app_models) if all_exist else "",
        "missing": missing,
        "models": MODELS_LIST,
    }


def download_all_models_sync(progress_cb: Callable[[dict], None] | None = None) -> None:
    """Tải lần lượt toàn bộ 3 model từ ModelScope về app models_cache."""
    global DOWNLOAD_STATE
    with _DOWNLOAD_LOCK:
        if DOWNLOAD_STATE["is_downloading"]:
            return
        DOWNLOAD_STATE.update({
            "is_downloading": True,
            "progress": 0.0,
            "error": "",
            "downloaded_mb": 0,
        })

    target_dir = str(get_models_cache_dir())
    os.environ["MODELSCOPE_CACHE"] = target_dir
    os.environ["HUB_CACHE"] = target_dir

    try:
        from pipeline.step04_stt import _UNPATCH_MODELSCOPE_FOR_DOWNLOAD
        _UNPATCH_MODELSCOPE_FOR_DOWNLOAD()
    except Exception:
        pass

    try:
        from modelscope.hub.snapshot_download import snapshot_download
        total_mb = sum(m["approx_mb"] for m in MODELS_LIST)
        done_mb = 0

        for i, m in enumerate(MODELS_LIST):
            with _DOWNLOAD_LOCK:
                DOWNLOAD_STATE["current_model"] = m["name"]
                DOWNLOAD_STATE["current_index"] = i + 1
                DOWNLOAD_STATE["progress"] = round(done_mb / total_mb, 2)
            if progress_cb:
                progress_cb(DOWNLOAD_STATE)

            logger.info("Downloading AI model %d/%d: %s (%s)...", i + 1, len(MODELS_LIST), m["name"], m["id"])
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    snapshot_download(
                        model_id=m["id"],
                        cache_dir=target_dir,
                        local_files_only=False,
                    )
                    break
                except Exception as dl_err:
                    if attempt == max_retries:
                        raise dl_err
                    logger.warning("Download %s failed (attempt %d/%d): %s. Retrying in 3s...", m["name"], attempt, max_retries, dl_err)
                    import time
                    time.sleep(3)

            done_mb += m["approx_mb"]
            with _DOWNLOAD_LOCK:
                DOWNLOAD_STATE["downloaded_mb"] = done_mb
                DOWNLOAD_STATE["progress"] = round(done_mb / total_mb, 2)
            if progress_cb:
                progress_cb(DOWNLOAD_STATE)

        with _DOWNLOAD_LOCK:
            DOWNLOAD_STATE["progress"] = 1.0
            DOWNLOAD_STATE["is_downloading"] = False
            DOWNLOAD_STATE["current_model"] = "Hoàn tất"
        if progress_cb:
            progress_cb(DOWNLOAD_STATE)
        logger.info("All AI models downloaded successfully.")

    except Exception as exc:
        logger.exception("Failed to download AI models: %s", exc)
        with _DOWNLOAD_LOCK:
            DOWNLOAD_STATE["is_downloading"] = False
            DOWNLOAD_STATE["error"] = str(exc)
        if progress_cb:
            progress_cb(DOWNLOAD_STATE)
        raise

