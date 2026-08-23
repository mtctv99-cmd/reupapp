"""TTS — hỗ trợ Google TTS (gTTS) + TikTok / Remote TTS Server.

Áp dụng Cooldown + Exponential Backoff Retry cho TOÀN BỘ các engine.
Mọi lỗi mạng (timeout, reset, HTTP 429/5xx, JSON hỏng, audio fail) đều được
retry; server fail sẽ fallback sang gTTS (Google — không bị rate limit như Edge).
"""
import asyncio
import base64
import logging
import os
import random
import tempfile
import time
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# Global Cooldown Lock cho tất cả request TTS (tối thiểu 0.5s giữa các câu)
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
    """Exponential backoff: 2^retry + jitter (tránh dồn request khi server quá tải)."""
    return (2 ** retry) + random.uniform(0.5, 1.5)


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
    import re
    t = re.sub(r'\s+', ' ', t).strip()
    return t


async def run_tts(text: str, voice: str = "") -> str:
    """Generate TTS từ voice được chọn. Retry 3 lần + backoff; hỏng dừng luôn, không fallback."""
    text = _clean_for_tts(text)
    v_norm = (voice or "").strip()

    if v_norm in ("bv74_steaming", "bv074_streaming", "tiktok_female"):
        v_norm = "BV074_streaming"
    elif v_norm in ("bv001_streaming", "tiktok_male"):
        v_norm = "BV001_streaming"
    elif not v_norm:
        v_norm = "BV074_streaming"

    # TikTok TTS native khi có session
    if v_norm in ("BV074_streaming", "BV001_streaming"):
        from core import tiktok
        if tiktok.has_session():
            out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            out.close()
            try:
                return await tiktok.generate(text, v_norm, out.name)
            except Exception as exc:
                if os.path.exists(out.name):
                    try:
                        os.remove(out.name)
                    except OSError:
                        pass
                raise RuntimeError(f"TikTok TTS failed after retries: {exc}") from exc

    servers = settings.get_tts_servers()
    if not servers:
        ttsp = settings.get("tts_provider", {})
        url = ttsp.get("url", "https://tts.maika.sbs")
        api_key = ttsp.get("api_key", "omni-secret-key-123")
        if url:
            servers = [{"url": url, "api_key": api_key, "voice": v_norm}]

    if not servers:
        servers = [{"url": "https://tts.maika.sbs", "api_key": "omni-secret-key-123", "voice": v_norm}]

    last_error = None
    for idx, server in enumerate(servers):
        url = server.get("url", "").rstrip("/")
        api_key = server.get("api_key", "")
        if not url:
            continue
        try:
            return await _tts_from_server(text, v_norm, url, api_key)
        except Exception as exc:
            last_error = exc
            logger.warning("TTS server %d with voice '%s' failed: %s", idx + 1, v_norm, exc)

    raise RuntimeError(f"TTS failed for voice '{v_norm}' after 3 retries: {last_error}")

def _save_gtts(text: str, out_path: str) -> None:
    from gtts import gTTS
    # ponytail: gTTS crashes on empty/whitespace-only text
    clean = (text or "").strip()
    if not clean:
        raise ValueError("gTTS: empty text")
    gTTS(text=clean, lang="vi", slow=False).save(out_path)

async def _run_gtts(text: str) -> str:
    """Google TTS (gTTS) — thay thế Edge TTS (hay bị rate limit)."""
    clean = (text or "").strip()
    if not clean:
        raise RuntimeError("gTTS: empty text, nothing to speak")
    last_err = None
    for attempt in range(3):
        out = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        out.close()
        try:
            await asyncio.to_thread(_save_gtts, clean, out.name)
            if os.path.exists(out.name) and os.path.getsize(out.name) > 500:
                return out.name
            last_err = RuntimeError("gTTS produced empty file")
        except Exception as err:
            last_err = err
            logger.warning("gTTS attempt %d/3 failed: %s", attempt + 1, err)
        if last_err:
            try:
                os.remove(out.name)
            except OSError:
                pass
        await asyncio.sleep(1.0)

    raise RuntimeError(f"gTTS failed: {last_err}")

async def _tts_from_server(text: str, voice: str, base_url: str, api_key: str) -> str:
    """Remote Server TTS với Cooldown + Retry Exponential Backoff (3 lần)."""
    import httpx
    from urllib.parse import urljoin

    headers = {"Content-Type": "application/json"}
    if api_key:
        clean_key = api_key.replace(" -", "-").replace("- ", "-").replace(" ", "")
        headers["X-API-Key"] = clean_key
        headers["Authorization"] = f"Bearer {clean_key}"

    payload = {"text": text, "voice_id": voice, "language": "vi"}
    last_err: Exception | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for retry in range(3):
            last_err = None
            await _apply_cooldown(0.5)
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
                logger.warning("Remote TTS POST failed attempt %d/3 (%s). Backoff %.2fs...",
                               retry + 1, exc, backoff)
                await asyncio.sleep(backoff)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                backoff = _backoff_sec(retry)
                logger.warning("Remote Server TTS HTTP %d on attempt %d/3. Backoff %.2fs...",
                               resp.status_code, retry + 1, backoff)
                await asyncio.sleep(backoff)
                continue

            try:
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                last_err = exc
                backoff = _backoff_sec(retry)
                logger.warning("Remote TTS bad response on attempt %d/3 (%s). Backoff %.2fs...",
                               retry + 1, exc, backoff)
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
                        logger.warning("Remote TTS audio save failed attempt %d/3 (%s). Backoff %.2fs...",
                                       retry + 1, exc, backoff)
                        await asyncio.sleep(backoff)
                        continue
                last_err = RuntimeError(f"No job_id in response: {body}")
                backoff = _backoff_sec(retry)
                logger.warning("Remote TTS no job_id on attempt %d/3: %s", retry + 1, body)
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
            logger.warning("Remote TTS poll failed on attempt %d/3 (%s). Backoff %.2fs...",
                           retry + 1, last_err, backoff)
            await asyncio.sleep(backoff)

    raise RuntimeError(f"TTS server failed after 3 retries: {last_err}")
