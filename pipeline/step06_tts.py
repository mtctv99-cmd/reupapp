"""TTS — Chỉ hỗ trợ TikTok TTS Local + TikTok TTS từ Server cấu hình.

Áp dụng Cooldown + Exponential Backoff Retry (2 lần) cho mỗi request.
"""
import asyncio
import base64
import logging
import os
import random
import re
import tempfile
import time
from typing import Any
from urllib.parse import urljoin

from core.config import settings

logger = logging.getLogger(__name__)

# Global Cooldown Lock cho tất cả request TTS (tối thiểu 0.2s giữa các câu)
_GLOBAL_TTS_LOCK = asyncio.Lock()
_LAST_TTS_REQ_TIME = 0.0


async def _apply_cooldown(min_gap_sec: float = 0.2):
    global _LAST_TTS_REQ_TIME
    sleep_dur = 0.0
    async with _GLOBAL_TTS_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_TTS_REQ_TIME
        if elapsed < min_gap_sec:
            sleep_dur = min_gap_sec - elapsed
        _LAST_TTS_REQ_TIME = now + sleep_dur
    if sleep_dur > 0:
        await asyncio.sleep(sleep_dur)


def _backoff_sec(retry: int) -> float:
    """Exponential backoff: 2^retry + jitter."""
    return (2 ** retry) + random.uniform(0.3, 1.0)


def _save_audio_data(audio_data: Any) -> str:
    """Lưu audio (base64 string hoặc bytes) ra file tạm .mp3, speedup x1.1, trả về path."""
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

    # Tự động tăng tốc x1.1 sau khi nhận audio TTS về
    try:
        from pydub import AudioSegment
        from pydub.effects import speedup
        AudioSegment.converter = settings.get("ffmpeg_path", "ffmpeg")
        seg = AudioSegment.from_file(out.name)
        if len(seg) > 200:
            fast_seg = speedup(seg, playback_speed=1.1)
            fast_seg.export(out.name, format="mp3")
    except Exception as exc:
        logger.warning("TTS x1.1 speedup skipped: %s", exc)

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
    if v_norm.lower() in ("bv74_steaming", "bv074_streaming", "tiktok_female", "female", ""):
        return "BV074_streaming"
    if v_norm.lower() in ("bv001_streaming", "tiktok_male", "male"):
        return "BV001_streaming"
    return v_norm


async def run_tts(text: str, voice: str = "", max_retries: int = 2) -> str:
    """Generate TTS từ voice được chọn.
    Chỉ sử dụng:
    1. TikTok TTS local (core.tiktok) khi có session.
    2. TikTok TTS từ Remote Server cấu hình sẵn.
    Thực hiện retry max_retries (mặc định 2) lần cho mỗi nguồn.
    """
    text = _clean_for_tts(text)
    if not text:
        raise ValueError("TTS: text is empty")

    v_norm = _normalize_voice(voice)
    last_error: Exception | None = None

    # 1. Thử TikTok TTS Local native nếu có session
    if v_norm in ("BV074_streaming", "BV001_streaming"):
        from core import tiktok
        if tiktok.has_session():
            for attempt in range(max_retries):
                out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                out.close()
                try:
                    res = await tiktok.generate(text, v_norm, out.name)
                    if res and os.path.exists(res) and os.path.getsize(res) > 200:
                        return res
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
            return await _tts_from_server(text, v_norm, url, api_key, max_retries=max_retries)
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
