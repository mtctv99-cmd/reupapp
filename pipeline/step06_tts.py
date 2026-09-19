"""TTS — Chỉ hỗ trợ TikTok TTS Local + TikTok TTS từ Server cấu hình.

Áp dụng Cooldown + Exponential Backoff Retry (2 lần) cho mỗi request.
"""
import asyncio
import base64
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from core.config import settings

logger = logging.getLogger(__name__)

_PIPER_VOICES: dict[str, Any] = {}
_PIPER_LOCK = threading.Lock()


def resolve_piper_model(voice_name: str = "ngoc_huyen") -> tuple[Path, Path] | None:
    """Tìm file .onnx và .onnx.json của model Piper."""
    candidate_bases: list[Path] = []

    # 1. Thư mục APPDATA (%APPDATA%/SeverDesktop/models/piper)
    try:
        from core.config import app_dir
        candidate_bases.append(app_dir() / "models" / "piper")
    except Exception:
        pass

    # 2. Frozen PyInstaller directory
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        candidate_bases.append(meipass / "models" / "piper")
        candidate_bases.append(meipass / "_internal" / "models" / "piper")
        exe_dir = Path(sys.executable).parent
        candidate_bases.append(exe_dir / "models" / "piper")
        candidate_bases.append(exe_dir / "_internal" / "models" / "piper")

    # 3. Thư mục mã nguồn dự án
    proj_root = Path(__file__).parent.parent
    candidate_bases.append(proj_root / "models" / "piper")

    for base in candidate_bases:
        model_file = base / voice_name / f"{voice_name}.onnx"
        config_file = base / voice_name / f"{voice_name}.onnx.json"
        if model_file.is_file() and config_file.is_file():
            return (model_file, config_file)

    return None


def get_piper_voice(voice_name: str = "ngoc_huyen"):
    """Tải hoặc lấy đối tượng PiperVoice đã cache trong bộ nhớ."""
    with _PIPER_LOCK:
        if voice_name in _PIPER_VOICES:
            return _PIPER_VOICES[voice_name]
        paths = resolve_piper_model(voice_name)
        if not paths:
            return None
        model_path, config_path = paths
        try:
            from piper import PiperVoice
            voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=False)
            _PIPER_VOICES[voice_name] = voice
            logger.info("Loaded local Piper TTS voice '%s' from %s", voice_name, model_path)
            return voice
        except Exception as exc:
            logger.error("Failed to load local Piper voice '%s': %s", voice_name, exc)
            return None


def _synthesize_piper_sync(voice_obj, text: str, output_wav: str) -> None:
    with wave.open(output_wav, "wb") as wav_file:
        voice_obj.synthesize_wav(text, wav_file, set_wav_format=True)


async def synthesize_piper_local(text: str, voice_name: str = "ngoc_huyen") -> str | None:
    """Tạo audio TTS qua model Piper chạy trực tiếp trên máy (không cần internet hay server)."""
    voice_obj = get_piper_voice(voice_name)
    if not voice_obj:
        return None
    fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        await asyncio.to_thread(_synthesize_piper_sync, voice_obj, text, tmp_wav)
        if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 200:
            return tmp_wav
    except Exception as exc:
        logger.error("Local Piper synthesis error: %s", exc)
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
    return None

# Global Cooldown Lock đã được gỡ bỏ theo yêu cầu
async def _apply_cooldown(min_gap_sec: float = 0.0):
    pass


def _backoff_sec(retry: int) -> float:
    """Exponential backoff: 2^retry + jitter."""
    return (2 ** retry) + random.uniform(0.3, 1.0)


def _save_audio_data(audio_data: Any) -> str:
    """Lưu audio (base64 string hoặc bytes) ra file tạm .mp3, trả về path."""
    if isinstance(audio_data, str):
        try:
            audio_data = base64.b64decode(audio_data)
        except Exception as exc:
            raise ValueError(f"Invalid base64 audio: {exc}") from exc
    if not isinstance(audio_data, (bytes, bytearray)):
        raise ValueError(f"Unexpected audio type: {type(audio_data).__name__}")
    out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    out.close()
    with open(out.name, "wb") as f:
        f.write(audio_data)

    return out.name


def _clean_for_tts(text: str) -> str:
    """Làm sạch văn bản trước khi đưa vào TTS: loại bỏ ngoặc kép, ký tự lạ, chuẩn hóa dấu câu."""
    t = str(text or "").strip()
    t = t.replace('“', '').replace('”', '').replace('"', '').replace("'", "")
    t = t.replace('「', '').replace('」', '').replace('『', '').replace('』', '')
    t = t.replace('《', '').replace('》', '').replace('【', '').replace('】', '')
    t = t.replace('（', '(').replace('）', ')')
    t = t.replace('，', ', ').replace('。', '. ').replace('！', '! ').replace('？', '? ')
    t = t.replace('：', ': ').replace('；', '; ')
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _normalize_voice(voice: str) -> str:
    v_norm = (voice or "").strip()
    if v_norm.lower() in ("ngoc_huyen", "ngochuyen", "huyen", "piper", "piper_ngoc_huyen"):
        return "ngoc_huyen"
    if v_norm.lower() in ("bv74_steaming", "bv074_streaming", "tiktok_female", "female"):
        return "BV074_streaming"
    if v_norm.lower() in ("bv001_streaming", "tiktok_male", "male"):
        return "BV001_streaming"
    if not v_norm:
        return "ngoc_huyen" if resolve_piper_model("ngoc_huyen") else "BV074_streaming"
    return v_norm


async def _apply_tts_speed(audio_path: str, speed: float = 1.05) -> str:
    """Tăng tốc âm thanh TTS (mặc định 1.05x) để giọng đọc gọn gàng, vừa vặn timeline."""
    if not audio_path or not os.path.exists(audio_path) or speed <= 0 or abs(speed - 1.0) < 0.01:
        return audio_path
    ffmpeg = settings.get("ffmpeg_path", "ffmpeg")
    ext = os.path.splitext(audio_path)[1] or ".wav"
    tmp_out = f"{audio_path}_speed_{speed}{ext}"
    cmd = [ffmpeg, "-y", "-i", audio_path, "-filter:a", f"atempo={speed:.2f}", tmp_out]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if proc.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 200:
            try:
                os.replace(tmp_out, audio_path)
                return audio_path
            except Exception:
                return tmp_out
    except Exception as exc:
        logger.warning("TTS atempo=%.2f failed: %s", speed, exc)
    finally:
        if os.path.exists(tmp_out) and tmp_out != audio_path:
            try:
                os.remove(tmp_out)
            except OSError:
                pass
    return audio_path


async def run_tts(text: str, voice: str = "", max_retries: int = 2, speed: float = 1.05) -> str:
    """Generate TTS từ voice được chọn kèm tốc độ mặc định 1.05x."""
    text = _clean_for_tts(text)
    if not text:
        raise ValueError("TTS: text is empty")

    if abs(speed - 1.05) < 0.001:
        try:
            speed = float(settings.get("tts_speed", 1.05))
        except (TypeError, ValueError):
            speed = 1.05

    v_norm = _normalize_voice(voice)
    last_error: Exception | None = None

    # 1. Thử Piper TTS Local trực tiếp (ưu tiên ngoc_huyen hoặc model local)
    if v_norm == "ngoc_huyen" or resolve_piper_model(v_norm):
        for attempt in range(max_retries):
            try:
                res = await synthesize_piper_local(text, v_norm)
                if res and os.path.exists(res) and os.path.getsize(res) > 200:
                    return await _apply_tts_speed(res, speed)
            except Exception as exc:
                last_error = exc
                logger.warning("Local Piper TTS attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1)
        logger.warning("Local Piper TTS failed for '%s', falling back...", v_norm)

    # 2. Thử TikTok TTS Local native nếu có session
    if v_norm in ("BV074_streaming", "BV001_streaming"):
        from core import tiktok
        if tiktok.has_session():
            for attempt in range(max_retries):
                out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                out.close()
                try:
                    res = await tiktok.generate(text, v_norm, out.name)
                    if res and os.path.exists(res) and os.path.getsize(res) > 200:
                        return await _apply_tts_speed(res, speed)
                except Exception as exc:
                    last_error = exc
                    logger.warning("Local TikTok TTS attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
                    if os.path.exists(out.name):
                        try:
                            os.remove(out.name)
                        except OSError:
                            pass
                    if attempt < max_retries - 1:
                        await asyncio.sleep(_backoff_sec(attempt))

    # 2. Thử Remote TikTok TTS Server
    servers = settings.get_tts_servers()
    if not servers:
        ttsp = settings.get("tts_provider", {})
        url = ttsp.get("url", "https://tts.maika.sbs")
        api_key = ttsp.get("api_key", "omni-secret-key-123")
        if url:
            servers = [{"url": url, "api_key": api_key, "voice": v_norm}]
    if not servers:
        servers = [{"url": "https://tts.maika.sbs", "api_key": "omni-secret-key-123", "voice": v_norm}]

    for idx, server in enumerate(servers):
        url = server.get("url", "").rstrip("/")
        api_key = server.get("api_key", "")
        if not url:
            continue
        try:
            res = await _tts_from_server(text, v_norm, url, api_key, max_retries=max_retries)
            if res and os.path.exists(res):
                return await _apply_tts_speed(res, speed)
        except Exception as exc:
            last_error = exc
            logger.warning("TTS server %d (%s) failed: %s", idx + 1, url, exc)

    raise RuntimeError(f"TTS failed for voice '{v_norm}' after retries: {last_error}")


async def _tts_from_server(text: str, voice: str, base_url: str, api_key: str, max_retries: int = 2) -> str:
    """Remote Server TikTok TTS với Cooldown + Retry (max_retries lần)."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if api_key:
        clean_key = api_key.replace(" -", "-").replace("- ", "-").replace(" ", "")
        headers["X-API-Key"] = clean_key
        headers["Authorization"] = f"Bearer {clean_key}"

    payload = {"text": text, "voice_id": voice, "language": "vi"}
    last_err: Exception | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for retry in range(max_retries):
            last_err = None
            await _apply_cooldown(0.3)
            try:
                resp = await client.post(f"{base_url}/api/tts/generate", json=payload, headers=headers)
                if resp.status_code == 404:
                    resp = await client.post(
                        f"{base_url}/v1/jobs",
                        json={"text": text, "voice_path": voice, "language": "vi", "quality": "thuong"},
                        headers=headers)
            except Exception as exc:
                last_err = exc
                backoff = _backoff_sec(retry)
                logger.warning("Remote TTS POST failed attempt %d/%d (%s). Backoff %.2fs...",
                               retry + 1, max_retries, exc, backoff)
                await asyncio.sleep(backoff)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                backoff = _backoff_sec(retry)
                logger.warning("Remote Server TTS HTTP %d on attempt %d/%d. Backoff %.2fs...",
                               resp.status_code, retry + 1, max_retries, backoff)
                await asyncio.sleep(backoff)
                continue

            try:
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                last_err = exc
                backoff = _backoff_sec(retry)
                logger.warning("Remote TTS bad response on attempt %d/%d (%s). Backoff %.2fs...",
                               retry + 1, max_retries, exc, backoff)
                await asyncio.sleep(backoff)
                continue

            job_id = body.get("job_id") if isinstance(body, dict) else None
            if not job_id:
                audio_data = body.get("audio") if isinstance(body, dict) else None
                if audio_data:
                    try:
                        return _save_audio_data(audio_data)
                    except Exception as exc:
                        last_err = exc
                        backoff = _backoff_sec(retry)
                        logger.warning("Remote TTS audio save failed attempt %d/%d (%s). Backoff %.2fs...",
                                       retry + 1, max_retries, exc, backoff)
                        await asyncio.sleep(backoff)
                        continue
                last_err = RuntimeError(f"No job_id in response: {body}")
                backoff = _backoff_sec(retry)
                logger.warning("Remote TTS no job_id on attempt %d/%d: %s", retry + 1, max_retries, body)
                await asyncio.sleep(backoff)
                continue

            status_url = urljoin(base_url + "/", str(body.get("status_url", f"/api/tts/jobs/{job_id}")).lstrip("/"))
            audio_url = urljoin(base_url + "/", str(body.get("audio_url", f"/api/tts/jobs/{job_id}/audio")).lstrip("/"))

            for _ in range(40):  # tối đa 20s mỗi attempt
                await asyncio.sleep(0.5)
                try:
                    s_resp = await client.get(status_url, headers=headers)
                    task = s_resp.json()
                except Exception:
                    continue
                if s_resp.status_code != 200:
                    continue
                st = str(task.get("status", "")).upper()
                if st in ("SUCCESS", "COMPLETED", "DONE"):
                    try:
                        a_resp = await client.get(audio_url, headers=headers)
                        a_resp.raise_for_status()
                    except Exception as exc:
                        last_err = exc
                        break
                    return _save_audio_data(a_resp.content)
                if st in ("FAILED", "ERROR"):
                    last_err = RuntimeError(f"TTS job {job_id} failed: {task.get('error')}")
                    break

            if last_err is None:
                last_err = RuntimeError(f"TTS job {job_id} did not finish in time")
            backoff = _backoff_sec(retry)
            logger.warning("Remote TTS poll failed on attempt %d/%d (%s). Backoff %.2fs...",
                           retry + 1, max_retries, last_err, backoff)
            await asyncio.sleep(backoff)

    raise RuntimeError(f"TTS server failed after {max_retries} retries: {last_err}")
