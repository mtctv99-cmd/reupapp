"""Sever Dub Studio Pro — FastAPI Backend.
Serve REST API + static UI + media preview. Pipeline runs in a dedicated thread.
"""
import asyncio
import glob
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import settings, app_dir
from core.db import create_job, list_jobs, get_job, update_job, delete_job, reset_stale_running_jobs

logger = logging.getLogger("sever")

app = FastAPI(title="Sever Dub Studio Pro")

@app.on_event("startup")
async def startup_event():
    import threading
    from core.config import ensure_env_paths
    ensure_env_paths()

    # Thu hồi các job bị kẹt trạng thái running khi app tắt đột ngột
    try:
        stale_count = reset_stale_running_jobs()
        if stale_count > 0:
            logger.info("Reset %d stale running jobs to failed status", stale_count)
    except Exception as exc:
        logger.warning("Failed to reset stale jobs: %s", exc)

    from pipeline.step04_stt import preload_stt_model
    threading.Thread(target=preload_stt_model, daemon=True, name="stt-preloader").start()
    threading.Thread(target=_queue_worker_loop, daemon=True, name="queue-worker").start()

    # ponytail: check required tools at startup
    import shutil
    missing = []
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        logger.warning("Missing tools not in PATH: %s — install them or set paths in Settings", ", ".join(missing))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PIPELINE_STATUS = {
    "running": False, "job_id": "", "step_index": 0,
    "step_name": "", "progress": 0.0, "error": "", "logs": [],
    "pending_count": 0,
}
CANCEL_EVENT = threading.Event()  # ponytail: kept for backward compat; new code uses _current_cancel_event
_current_cancel_event = threading.Event()
_QUEUE_TRIGGER = threading.Event()
_STATUS_LOCK = threading.Lock()  # ponytail: global lock, fine at this scale


def _queue_worker_loop():
    """Background worker continuously executing pending jobs from SQLite."""
    from core.db import get_next_pending_job, count_pending_jobs
    global _current_cancel_event

    while True:
        try:
            job = get_next_pending_job()
            if not job:
                with _STATUS_LOCK:
                    PIPELINE_STATUS["pending_count"] = 0
                    if not PIPELINE_STATUS["running"]:
                        PIPELINE_STATUS["step_name"] = "Ready"
                _QUEUE_TRIGGER.clear()
                _QUEUE_TRIGGER.wait(timeout=2.0)
                continue

            # Found pending job -> execute it
            job_id = job["id"]
            url = job.get("source_url", "")
            title = job.get("title", "")
            source_lang = job.get("source_language", "zh")
            target_lang = job.get("target_language", "vi")
            job_settings = {}
            try:
                if job.get("settings_json"):
                    job_settings = json.loads(job["settings_json"]) if isinstance(job["settings_json"], str) else job["settings_json"]
            except Exception:
                pass

            voice = job_settings.get("voice", "") or settings.get("voice", "")
            remove_sub = True

            # Sync settings into runtime
            for k in ("aspect_ratio", "bg_style", "bg_color", "logo_path", "logo_position", "logo_opacity", "sub_style", "sub_font_size", "video_speed", "original_volume", "output_dir"):
                if k in job_settings:
                    settings.set(k, job_settings[k])

            _current_cancel_event = threading.Event()
            pending_c = count_pending_jobs()

            with _STATUS_LOCK:
                PIPELINE_STATUS.update({
                    "running": True,
                    "job_id": job_id,
                    "step_index": 1,
                    "step_name": "Starting...",
                    "progress": 0.0,
                    "error": "",
                    "logs": [f"[{time.strftime('%H:%M:%S')}] Bắt đầu job {job_id}: {title or url}"],
                    "pending_count": pending_c,
                })

            _run_pipeline_job(job_id, url, source_lang, target_lang, voice, remove_sub, title, _current_cancel_event)

        except Exception as exc:
            logger.exception("Error in queue worker loop: %s", exc)
            time.sleep(1.0)


def _run_pipeline_job(job_id: str, url: str, source_lang: str, target_lang: str, voice: str, remove_sub: bool, title: str, cancel_event: threading.Event):
    from pipeline.orchestrator import PipelineOrchestrator
    from core.db import count_pending_jobs
    orch = PipelineOrchestrator(
        source_url=url, source_lang=source_lang, target_lang=target_lang,
        voice=voice, remove_sub=remove_sub, title=title, job_id=job_id,
        on_progress=on_progress_callback, cancel_event=cancel_event,
    )
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(orch.run())
        with _STATUS_LOCK:
            PIPELINE_STATUS["job_id"] = orch.job_id
            PIPELINE_STATUS.update(running=False, progress=1.0, step_name="Completed", pending_count=count_pending_jobs())
            PIPELINE_STATUS["logs"].append(f"[{time.strftime('%H:%M:%S')}] ✅ Hoàn tất")
    except Exception as exc:
        with _STATUS_LOCK:
            PIPELINE_STATUS["job_id"] = orch.job_id
            PIPELINE_STATUS.update(running=False, error=str(exc), pending_count=count_pending_jobs())
            PIPELINE_STATUS["logs"].append(f"[{time.strftime('%H:%M:%S')}] ❌ Lỗi: {exc}")
        logger.exception("pipeline failed: %s", exc)
    finally:
        if loop is not None:
            loop.close()
        time.sleep(0.5)


def _src_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "src")


def _allowed_roots() -> list[str]:
    roots = [
        settings.get("output_dir", ""),
        settings.get("download_dir", ""),
        str(app_dir()),
        os.getcwd(),
        os.path.join(os.getcwd(), "output"),
        os.path.join(os.getcwd(), "downloads"),
    ]
    return [os.path.normcase(os.path.abspath(r)) for r in roots if r]


async def on_progress_callback(step_index: int, step_name: str, sub_progress: float):
    with _STATUS_LOCK:
        overall = ((step_index - 1) + sub_progress) / 8.0
        PIPELINE_STATUS["step_index"] = step_index
        PIPELINE_STATUS["step_name"] = step_name
        PIPELINE_STATUS["progress"] = round(overall, 3)
        # ponytail: callback bắn rất dày (mỗi chunk) → chỉ ghi log khi dòng đổi,
        # tránh 200 dòng trùng nhau nuốt hết lịch sử các bước trước.
        line = f"[{time.strftime('%H:%M:%S')}] Bước {step_index}/8 · {step_name} · {int(sub_progress * 100)}%"
        logs = PIPELINE_STATUS["logs"]
        if not logs or logs[-1][11:] != line[11:]:
            logs.append(line)
            if len(logs) > 200:
                del logs[:-200]


def _run_pipeline(url: str, source_lang: str, target_lang: str, voice: str, remove_sub: bool, title: str = "", cancel_event: threading.Event | None = None):
    from pipeline.orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator(
        source_url=url, source_lang=source_lang, target_lang=target_lang,
        voice=voice, remove_sub=remove_sub, title=title,
        on_progress=on_progress_callback, cancel_event=cancel_event or CANCEL_EVENT,
    )
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run_and_track():
            res = await orch.run()
            with _STATUS_LOCK:
                PIPELINE_STATUS["job_id"] = orch.job_id
            return res

        loop.run_until_complete(_run_and_track())
        with _STATUS_LOCK:
            PIPELINE_STATUS["job_id"] = orch.job_id
            PIPELINE_STATUS.update(running=False, progress=1.0, step_name="Completed")
            PIPELINE_STATUS["logs"].append(f"[{time.strftime('%H:%M:%S')}] ✅ Hoàn tất")
    except Exception as exc:
        with _STATUS_LOCK:
            PIPELINE_STATUS["job_id"] = orch.job_id
            PIPELINE_STATUS.update(running=False, error=str(exc))
            PIPELINE_STATUS["logs"].append(f"[{time.strftime('%H:%M:%S')}] ❌ Lỗi: {exc}")
        logger.exception("pipeline failed: %s", exc)
    finally:
        if loop is not None:
            loop.close()


# ── GET ─────────────────────────────────────────────────────────

@app.get("/api/version")
async def api_version():
    from core import __version__
    return {"status": "ok", "version": __version__}


@app.get("/api/check-update")
async def api_check_update():
    """Kiểm tra bản cập nhật mới từ GitHub Releases."""
    from core import __version__
    import httpx
    
    release_repo = settings.get("release_repo", "mtctv99-cmd/reupapp")
    url = f"https://api.github.com/repos/{release_repo}/releases/latest"
    custom_manifest_url = settings.get("update_manifest_url", "")
    target_url = custom_manifest_url if custom_manifest_url else url

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(target_url, headers={"User-Agent": "SeverStudioApp"})
            if resp.status_code == 200:
                data = resp.json()
                latest_tag = str(data.get("tag_name", "")).lstrip("v").strip()
                html_url = data.get("html_url", "")
                download_url = html_url
                for asset in data.get("assets", []):
                    aname = asset.get("name", "").lower()
                    if aname.endswith(".zip") or aname.endswith(".exe") or aname.endswith(".rar"):
                        download_url = asset.get("browser_download_url", download_url)
                        break

                def _is_newer(latest: str, current: str) -> bool:
                    try:
                        from packaging import version
                        return version.parse(latest) > version.parse(current)
                    except Exception:
                        return bool(latest and latest != current)

                has_update = _is_newer(latest_tag, __version__)
                return {
                    "status": "ok",
                    "has_update": has_update,
                    "current_version": __version__,
                    "latest_version": latest_tag,
                    "download_url": download_url,
                    "title": data.get("name", f"Phiên bản {latest_tag}"),
                    "changelog": data.get("body", ""),
                }
    except Exception as exc:
        logger.debug("Check update error: %s", exc)

    return {
        "status": "ok",
        "has_update": False,
        "current_version": __version__,
        "latest_version": __version__,
        "download_url": "",
        "changelog": "",
    }


@app.get("/api/models/status")
async def api_models_status():
    """Kiểm tra trạng thái Model AI và Binaries trên máy."""
    from core.models import check_models_installed, check_binaries_installed, DOWNLOAD_STATE
    installed = check_models_installed()
    bins = check_binaries_installed()
    return {
        "status": "ok",
        "ready": installed["ready"] and bins["ready"],
        "models_ready": installed["ready"],
        "binaries_ready": bins["ready"],
        "location": installed.get("location", ""),
        "missing": installed.get("missing", []),
        "missing_binaries": bins.get("missing", []),
        "downloading": DOWNLOAD_STATE["is_downloading"],
        "progress": DOWNLOAD_STATE["progress"],
    }


@app.post("/api/models/download")
async def api_models_download():
    """Kích hoạt tải Model AI & Binaries nền về %APPDATA%/SeverDesktop/."""
    from core.models import (
        download_all_models_sync,
        download_missing_binaries,
        check_binaries_installed,
        check_models_installed,
        DOWNLOAD_STATE,
    )
    if DOWNLOAD_STATE["is_downloading"]:
        return {"status": "ok", "message": "Đang tải", "downloading": True}

    def _bg_download():
        DOWNLOAD_STATE["error"] = ""
        DOWNLOAD_STATE["is_downloading"] = True
        DOWNLOAD_STATE["current_model"] = "Kiểm tra dữ liệu..."
        try:
            # 1. Tải Binaries trước nếu thiếu (nhanh, ~100-200MB)
            if not check_binaries_installed()["ready"]:
                errors = download_missing_binaries()
                if errors:
                    logger.warning("Binary download incomplete: %s", errors)
            # 2. Tải STT Models (bỏ qua nếu đã đủ)
            if not check_models_installed()["ready"]:
                download_all_models_sync(already_marked=True)
        except Exception as exc:
            logger.error("Background AI model/binaries download failed: %s", exc)
            DOWNLOAD_STATE["error"] = f"Tải dữ liệu lỗi: {exc}"
        finally:
            DOWNLOAD_STATE["is_downloading"] = False
            if not DOWNLOAD_STATE["error"]:
                DOWNLOAD_STATE["current_model"] = "Hoàn tất"

    threading.Thread(target=_bg_download, daemon=True, name="model-downloader").start()
    return {"status": "ok", "message": "Bắt đầu tải", "downloading": True}


@app.get("/api/models/progress")
async def api_models_progress():
    """Truy vấn tiến trình tải Model AI."""
    from core.models import DOWNLOAD_STATE
    return {"status": "ok", "data": dict(DOWNLOAD_STATE)}


@app.get("/api/jobs")
async def api_jobs():
    return {"status": "ok", "data": list_jobs(50)}


@app.get("/api/jobs/detail")
async def api_jobs_detail(id: str = ""):
    return {"status": "ok", "data": get_job(id) if id else None}


@app.post("/api/choose-folder")
async def api_choose_folder():
    """Mở hộp thoại Windows Folder Picker và lưu thư mục xuất bản."""
    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="Chọn thư mục lưu video kết quả")
            root.destroy()
            return folder
        except Exception as e:
            logger.warning("Folder picker error: %s", e)
            return None

    folder = await asyncio.to_thread(_pick)
    if folder:
        folder = os.path.normpath(folder)
        settings.set("output_dir", folder)
        return {"status": "ok", "folder": folder}
    return {"status": "cancelled", "folder": settings.get("output_dir", "")}


@app.post("/api/fetch-title")
async def api_fetch_title(payload: dict):
    """Lấy tiêu đề gốc từ video URL và tự động dịch sang Tiếng Việt."""
    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "URL required"}, 400)

    from pipeline.step01_download import _fetch_title_ytdlp
    from pipeline.step05_translate import translate_tier

    raw_title = ""
    try:
        raw_title = await _fetch_title_ytdlp(url)
    except Exception as exc:
        logger.warning("Failed to fetch title: %s", exc)

    if raw_title and "\n" in raw_title:
        lines = [ln.strip() for ln in raw_title.split("\n") if ln.strip() and not ln.strip().lower().startswith("p0")]
        raw_title = lines[0] if lines else raw_title.split("\n")[0]

    vi_title = raw_title
    if raw_title and raw_title.lower() not in ("video bilibili", "bilibili_video", "bilibili video", "bilibili"):
        tiers = settings.get_translate_tiers()
        if tiers:
            try:
                res = await translate_tier([raw_title], tiers[0], "zh", "vi")
                if res and res[0]:
                    vi_title = res[0].strip()
            except Exception as exc:
                logger.warning("Failed to translate title: %s", exc)

    return {"status": "ok", "raw_title": raw_title, "title": vi_title}


# ── POST ────────────────────────────────────────────────────────

@app.post("/api/jobs/create")
async def api_jobs_create(payload: dict):
    from core.db import create_job, count_pending_jobs
    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "Video URL is required"}, 400)

    title = str(payload.get("title", "")).strip()
    source_lang = str(payload.get("source_lang", "zh")).strip()
    target_lang = str(payload.get("target_lang", "vi")).strip()
    voice = str(payload.get("voice", "")).strip()
    video_speed = float(payload.get("video_speed", 1.0))
    original_volume = int(payload.get("original_volume", 100))
    output_dir = str(payload.get("output_dir", "")).strip()

    job_settings = {
        "voice": voice,
        "video_speed": video_speed,
        "original_volume": original_volume,
        "output_dir": output_dir or settings.get("output_dir", ""),
        "aspect_ratio": payload.get("aspect_ratio", settings.get("aspect_ratio", "original")),
        "bg_style": payload.get("bg_style", settings.get("bg_style", "blur")),
        "bg_color": payload.get("bg_color", settings.get("bg_color", "black")),
        "sub_style": payload.get("sub_style", settings.get("sub_style", "yellow_black")),
        "sub_font_size": payload.get("sub_font_size", settings.get("sub_font_size", 38)),
        "logo_path": payload.get("logo_path", settings.get("logo_path", "")),
        "logo_position": payload.get("logo_position", settings.get("logo_position", "top_right")),
        "logo_opacity": payload.get("logo_opacity", settings.get("logo_opacity", 0.8)),
    }

    # Tiêu đề tạm hiển thị ngay lập tức (phản hồi < 5ms, không bị khựng)
    placeholder_title = title or ("Video " + (re.search(r'(BV[a-zA-Z0-9]+)', url).group(1) if re.search(r'(BV[a-zA-Z0-9]+)', url) else "Thuyết Minh"))
    job_id = create_job(url, title=placeholder_title, source_lang=source_lang, target_lang=target_lang, settings_override=job_settings)
    _QUEUE_TRIGGER.set()

    # Nếu chưa có tiêu đề, chạy background task ngầm tự cập nhật tiêu đề không chặn UI
    if not title:
        async def _bg_fetch_title(jid: str, vurl: str, slang: str, tlang: str):
            try:
                from pipeline.step01_download import _fetch_title_ytdlp
                from pipeline.step05_translate import translate_tier
                from core.db import update_job
                raw_title = await _fetch_title_ytdlp(vurl)
                if raw_title:
                    if "\n" in raw_title:
                        lines = [ln.strip() for ln in raw_title.split("\n") if ln.strip() and not ln.strip().lower().startswith("p0")]
                        raw_title = lines[0] if lines else raw_title.split("\n")[0]
                    v_title = raw_title
                    tiers = settings.get_translate_tiers()
                    if tiers and slang != tlang:
                        res = await translate_tier([raw_title], tiers[0], slang, tlang)
                        if res and res[0]:
                            v_title = res[0].strip()
                    update_job(jid, title=v_title)
            except Exception as exc:
                logger.debug("Background title fetch: %s", exc)

        asyncio.create_task(_bg_fetch_title(job_id, url, source_lang, target_lang))

    with _STATUS_LOCK:
        is_running = PIPELINE_STATUS["running"]
        pending_c = count_pending_jobs()
        PIPELINE_STATUS["pending_count"] = pending_c

    msg = "Đã thêm vào hàng đợi" if is_running else "Đã khởi chạy"
    return {"status": "ok", "message": msg, "job_id": job_id, "is_running": is_running, "pending_count": pending_c}


@app.get("/api/jobs/output")
async def api_jobs_output(id: str = ""):
    job = get_job(id) if id else None
    result = {"output_path": "", "output_dir": "", "exists": False}
    if job:
        out_dir = job.get("output_dir", "")
        result["output_dir"] = out_dir
        if out_dir and os.path.isdir(out_dir):
            manifest = os.path.join(out_dir, f"{id}_manifest.json")
            if os.path.exists(manifest):
                try:
                    m = json.loads(Path(manifest).read_text("utf-8"))
                    p = m.get("output_path", "")
                    if p and os.path.exists(p):
                        result.update({"output_path": p, "exists": True,
                                       "size_bytes": m.get("size_bytes"),
                                       "duration_s": m.get("duration_s")})
                except Exception:
                    pass
            if not result["exists"]:
                # Tìm đúng file mp4 chứa ID của job này
                matched_id = glob.glob(os.path.join(out_dir, f"*{id}*.mp4"))
                if matched_id:
                    result["output_path"] = matched_id[0]
                    result["exists"] = True
                else:
                    mp4s = sorted(glob.glob(os.path.join(out_dir, "*.mp4")),
                                  key=os.path.getmtime, reverse=True)
                    if mp4s:
                        result["output_path"] = mp4s[0]
                        result["exists"] = True
        # Fallback check in project default output dir if job output_dir didn't yield file
        if not result["exists"]:
            default_out = os.path.join(os.getcwd(), "output")
            if os.path.isdir(default_out):
                matched = glob.glob(os.path.join(default_out, f"*{id}*.mp4"))
                if matched:
                    result["output_path"] = matched[0]
                    result["output_dir"] = default_out
                    result["exists"] = True
    return {"status": "ok", "data": result}


@app.get("/api/settings")
async def api_settings():
    return {"status": "ok", "data": settings.data}


@app.get("/api/progress")
async def api_progress():
    with _STATUS_LOCK:
        snapshot = dict(PIPELINE_STATUS)
    return {"status": "ok", "data": snapshot}


@app.get("/media")
async def media(path: str = ""):
    if not path:
        return JSONResponse({"error": "path required"}, 400)
    norm_p = os.path.normcase(os.path.abspath(path))
    allowed = _allowed_roots()
    if not any(norm_p.startswith(r + os.sep) or norm_p == r for r in allowed):
        # Fallback if inside workspace
        cwd_norm = os.path.normcase(os.path.abspath(os.getcwd()))
        if not norm_p.startswith(cwd_norm):
            return JSONResponse({"error": "forbidden"}, 403)
    if not os.path.isfile(norm_p):
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(norm_p)


# ── POST ────────────────────────────────────────────────────────

@app.post("/api/jobs/cancel")
async def api_jobs_cancel(payload: dict | None = None):
    target_id = ""
    if payload and isinstance(payload, dict):
        target_id = str(payload.get("id", "")).strip()

    with _STATUS_LOCK:
        running_id = PIPELINE_STATUS.get("job_id", "")
        is_running = PIPELINE_STATUS.get("running", False)

    # If cancelling currently running job
    if not target_id or target_id == running_id:
        _current_cancel_event.set()
        with _STATUS_LOCK:
            PIPELINE_STATUS.update(running=False, step_name="Cancelled")
        if running_id:
            update_job(running_id, status="cancelled")
        return {"status": "ok", "message": "Cancelled running job"}

    # If cancelling a pending job in queue
    job = get_job(target_id)
    if job:
        update_job(target_id, status="cancelled")
        return {"status": "ok", "message": f"Cancelled pending job {target_id}"}
    return {"status": "ok", "message": "Job not found"}


@app.post("/api/settings")
async def api_settings_save(payload: dict):
    for k, v in payload.items():
        settings.set(k, v)
    return {"status": "ok", "data": settings.data}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    uploads = os.path.join(app_dir(), "uploads")
    os.makedirs(uploads, exist_ok=True)
    name = os.path.basename(file.filename or "file")
    dest = os.path.join(uploads, f"{uuid.uuid4().hex[:8]}_{name}")
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
    except Exception as exc:
        return JSONResponse({"error": f"Upload failed: {exc}"}, 500)
    return {"status": "ok", "path": dest}


@app.post("/api/translate/models")
async def api_translate_models(payload: dict):
    provider_url = str(payload.get("url", "")).strip().rstrip("/")
    api_key = str(payload.get("api_key", "")).strip()
    models, error_msg = [], ""

    if provider_url:
        endpoints = [
            provider_url if provider_url.endswith(("/models", "/v1/models")) else None,
            f"{provider_url}/v1/models", f"{provider_url}/models",
            f"{provider_url}/api/models", provider_url,
        ]
        endpoints = [e for e in endpoints if e]

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        for ep in endpoints:
            try:
                req = urllib.request.Request(ep, headers=headers)
                with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw = data.get("data") if isinstance(data, dict) else data if isinstance(data, list) else data.get("models", []) if isinstance(data, dict) else []
                    for m in (raw if isinstance(raw, list) else []):
                        models.append(m.get("id") or m.get("name") if isinstance(m, dict) else str(m))
                    if models:
                        break
            except Exception as err:
                error_msg = f"Failed to fetch models from {ep}: {err}"

    return {"status": "ok", "data": models, "error": error_msg}


@app.post("/api/tts/voices")
async def api_tts_voices(payload: dict):
    tts_url = str(payload.get("url", "")).strip().rstrip("/")
    api_key = str(payload.get("api_key", "")).strip()
    normalized_key = api_key.replace(" -", "-").replace("- ", "-").replace(" ", "") if api_key else ""
    voices, error_msg = [], ""

    if tts_url:
        endpoints = [
            f"{tts_url}/api/tts/voices",
            tts_url if tts_url.endswith(("/voices", "/v1/voices", "/api/voices")) else None,
            f"{tts_url}/v1/voices", f"{tts_url}/voices",
            f"{tts_url}/api/voices", tts_url,
        ]
        endpoints = [e for e in endpoints if e]

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key

        headers_norm = None
        if normalized_key and normalized_key != api_key:
            headers_norm = {**headers, "Authorization": f"Bearer {normalized_key}", "X-API-Key": normalized_key}

        for ep in endpoints:
            for h in ([headers] + ([headers_norm] if headers_norm else [])):
                try:
                    req = urllib.request.Request(ep, headers=h)
                    with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        raw = data if isinstance(data, list) else data.get("voices") or data.get("data") or data.get("items") or []
                        for v in (raw if isinstance(raw, list) else []):
                            voices.append(v.get("voice_id") or v.get("name") or v.get("id") or str(v) if isinstance(v, dict) else str(v))
                        if voices:
                            break
                except Exception as err:
                    error_msg = f"Failed to fetch voices from {ep}: {err}"
            if voices:
                break

    return {"status": "ok", "data": voices, "error": error_msg, "normalized_key": normalized_key}


@app.post("/api/tools/concat")
async def api_tools_concat(payload: dict):
    files = payload.get("files", [])
    if not files or len(files) < 2:
        return JSONResponse({"error": "Vui lòng chọn ít nhất 2 video để ghép."}, 400)

    out_dir = settings.get("output_dir", "")
    os.makedirs(out_dir, exist_ok=True)
    output_path = os.path.join(out_dir, f"concat_{uuid.uuid4().hex[:8]}.mp4")

    from core.ffmpeg import concat_files
    try:
        await concat_files(files, output_path)
        return {"status": "ok", "message": "Ghép video thành công!", "output_path": output_path}
    except Exception as exc:
        return JSONResponse({"error": f"Lỗi ghép video: {str(exc)}"}, 500)


# ── DELETE ──────────────────────────────────────────────────────

@app.delete("/api/jobs")
async def api_jobs_delete(id: str = ""):
    if id:
        delete_job(id)
        return {"status": "ok", "message": "Deleted"}
    return JSONResponse({"error": "id required"}, 400)


# ── OPEN FOLDER ──────────────────────────────────────────────────


@app.get("/api/tiktok/login-status")
async def api_tiktok_login_status():
    from core import tiktok
    accounts = [
        {"name": a.get("name", ""), "preview": f"{a.get('sessionid', '')[:6]}...",
         "sessionid": a.get("sessionid", ""), "added_at": a.get("added_at", "")}
        for a in tiktok.list_accounts()
    ]
    return {"status": "ok", "has_session": tiktok.has_session(), "accounts": accounts}


@app.post("/api/tiktok/open-browser")
async def api_tiktok_open_browser(payload: dict = None):
    """Mở Chrome cho user đăng nhập; cookie tự lưu vào danh sách account."""
    import threading
    from core import tiktok

    slot = str((payload or {}).get("slot", "")).strip()
    threading.Thread(
        target=lambda: tiktok.login_blocking(slot),
        daemon=True, name="tiktok-login",
    ).start()
    return {"status": "ok", "message": "Đã mở trình duyệt. Đăng nhập xong cookie sẽ tự lưu."}


@app.post("/api/tiktok/sync-cookie")
async def api_tiktok_sync_cookie(payload: dict):
    """Nhập sessionid thủ công."""
    from core import tiktok
    sessionid = str(payload.get("sessionid", "")).strip()
    if not sessionid:
        return JSONResponse({"error": "Thiếu sessionid"}, 400)
    rec = tiktok.save_account(sessionid, str(payload.get("name", "")).strip())
    return {"status": "ok", "message": "Đã lưu TikTok Session ID!", "account": rec}


@app.delete("/api/tiktok/account")
async def api_tiktok_delete_account(sessionid: str = ""):
    from core import tiktok
    if not sessionid:
        return JSONResponse({"error": "Thiếu sessionid"}, 400)
    tiktok.delete_account(sessionid)
    return {"status": "ok", "message": "Đã xóa tài khoản"}


# ── Google (Gemini) Login ────────────────────────────────────────

@app.get("/api/google/accounts")
async def api_google_accounts():
    from core import google_auth
    return {"status": "ok", "accounts": google_auth.public_list()}


@app.post("/api/google/open-browser")
async def api_google_open_browser(payload: dict = None):
    """Mở trình duyệt cho user đăng nhập Google; cookie + token tự lưu rồi browser tự đóng."""
    import threading
    from core import google_auth

    threading.Thread(
        target=lambda: google_auth.login_blocking(),
        daemon=True, name="google-login",
    ).start()
    return {"status": "ok", "message": "Đã mở trình duyệt. Đăng nhập xong app tự lấy cookie/token và đóng cửa sổ."}


@app.delete("/api/google/account")
async def api_google_delete_account(id: str = ""):
    from core import google_auth
    if not id:
        return JSONResponse({"error": "Thiếu id tài khoản"}, 400)
    google_auth.delete_account(id)
    return {"status": "ok", "message": "Đã xóa tài khoản Google"}


# ── Bilibili Login ───────────────────────────────────────────────

@app.get("/api/bilibili/status")
async def api_bilibili_status():
    from core.bilibili_auth import check_login_status, load_cookie
    cookie_dir = str(app_dir())
    cookie = load_cookie(cookie_dir) or settings.get("bbdown_cookie", "")
    result = check_login_status(cookie)
    return {"status": "ok", "data": result}


@app.get("/api/bilibili/qrcode")
async def api_bilibili_qrcode():
    from core.bilibili_auth import generate_qrcode
    try:
        data = generate_qrcode()
        return {"status": "ok", "data": data}
    except Exception as exc:
        return JSONResponse({"error": f"Không tạo được QR: {exc}"}, 500)


@app.post("/api/bilibili/poll")
async def api_bilibili_poll(payload: dict):
    from core.bilibili_auth import poll_qrcode, parse_cookies_from_url, save_cookie
    key = str(payload.get("qrcode_key", "")).strip()
    if not key:
        return JSONResponse({"error": "qrcode_key required"}, 400)

    result = poll_qrcode(key)
    if result["status"] == "scan_success":
        url = result.get("url", "")
        cookie_str = parse_cookies_from_url(url)
        if cookie_str:
            cookie_path = save_cookie(cookie_str, str(app_dir()))
            settings.set("bbdown_cookie", cookie_path)
            result["cookie_path"] = cookie_path
    return {"status": "ok", "data": result}


@app.post("/api/bilibili/logout")
async def api_bilibili_logout():
    from pathlib import Path as _P
    cookie_file = _P(app_dir()) / "bilibili_cookies.txt"
    if cookie_file.exists():
        cookie_file.unlink()
    settings.set("bbdown_cookie", "")
    return {"status": "ok", "message": "Đã đăng xuất"}


@app.post("/api/bilibili/cookie")
async def api_bilibili_cookie_manual(payload: dict):
    """Save manual cookie input."""
    from core.bilibili_auth import save_cookie
    cookie_str = str(payload.get("cookie", "")).strip()
    if not cookie_str:
        return JSONResponse({"error": "Cookie required"}, 400)
    cookie_path = save_cookie(cookie_str, str(app_dir()))
    settings.set("bbdown_cookie", cookie_path)
    from core.bilibili_auth import check_login_status
    status = check_login_status(cookie_str)
    return {"status": "ok", "data": status, "cookie_path": cookie_path}

@app.post("/api/open-folder")
async def api_open_folder(payload: dict = None):
    import subprocess
    folder = (payload or {}).get("folder_path", "") or settings.get("output_dir", "")
    if not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

    try:
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"status": "ok", "message": f"Opened {folder}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/open-file")
async def api_open_file(payload: dict = None):
    import subprocess
    file_path = (payload or {}).get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, 404)

    try:
        if sys.platform == "win32":
            os.startfile(file_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", file_path])
        else:
            subprocess.Popen(["xdg-open", file_path])
        return {"status": "ok", "message": f"Opened {file_path}"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


# ── Static UI (mount last so /api/* takes precedence) ──────────
app.mount("/", StaticFiles(directory=_src_dir(), html=True), name="ui")