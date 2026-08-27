"""TikTok TTS engine + đăng nhập lấy cookie (đa tài khoản).

Port từ sever_v2 nhưng bỏ Redis/SQLite: pool xoay vòng cục bộ, account lưu
trong settings.json (`tiktok_accounts`).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx

from core.config import app_dir, settings

logger = logging.getLogger(__name__)

API_ENDPOINTS = [
    "https://api16-normal-c-useast1a.tiktokv.com/media/api/text/speech/invoke/",
    "https://api16-normal-useast5.us.tiktokv.com/media/api/text/speech/invoke/",
    "https://api16-normal-v6.tiktokv.com/media/api/text/speech/invoke/",
    "https://api22-core-c-alisg.tiktokv.com/media/api/text/speech/invoke/",
]
USER_AGENT = (
    "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 10; en_US; SM-G981B; "
    "Build/QP1A.190711.020;tt-ok/3.12.13.1)"
)
MAX_TEXT_CHARS = 700
MAX_TEXT_BYTES = 300  # tiếng Việt có dấu ~2-3 byte/ký tự → giới hạn thật ~100-150 chữ
COOLDOWN_SEC = 300.0

_COOLDOWN: dict[str, float] = {}  # sessionid -> thời điểm hết phạt (monotonic)
_RR = {"i": 0}


class TikTokRateLimited(RuntimeError):
    pass


# ── Accounts ─────────────────────────────────────────────────────

def list_accounts() -> list[dict]:
    return list(settings.get("tiktok_accounts", []) or [])


def save_account(sessionid: str, name: str = "") -> dict:
    """Thêm/cập nhật account theo sessionid; trả về bản ghi."""
    sessionid = (sessionid or "").strip()
    if not sessionid:
        raise ValueError("sessionid rỗng")
    accounts = list_accounts()
    rec = next((a for a in accounts if a.get("sessionid") == sessionid), None)
    if rec is None:
        rec = {"sessionid": sessionid}
        accounts.append(rec)
    rec["name"] = name or rec.get("name") or f"TikTok {len(accounts)}"
    rec["added_at"] = time.strftime("%Y-%m-%d %H:%M")
    settings.set("tiktok_accounts", accounts)
    settings.set("tiktok_sessionid", sessionid)  # tương thích code cũ
    return rec


def delete_account(sessionid: str) -> None:
    accounts = [a for a in list_accounts() if a.get("sessionid") != sessionid]
    settings.set("tiktok_accounts", accounts)
    if settings.get("tiktok_sessionid", "") == sessionid:
        settings.set("tiktok_sessionid", accounts[0]["sessionid"] if accounts else "")


def _session_ids() -> list[str]:
    ids = [a.get("sessionid", "") for a in list_accounts() if a.get("sessionid")]
    if not ids:
        legacy = settings.get("tiktok_sessionid", "")
        if legacy:
            ids = [legacy]
    now = time.monotonic()
    alive = [s for s in ids if _COOLDOWN.get(s, 0.0) <= now]
    return alive or ids  # hết sạch account "khỏe" thì vẫn thử, còn hơn fail ngay


def _next_session(offset: int) -> str | None:
    ids = _session_ids()
    if not ids:
        return None
    i = (_RR["i"] + offset) % len(ids)
    _RR["i"] = (_RR["i"] + 1) % len(ids)
    return ids[i]


def has_session() -> bool:
    return bool(_session_ids())


# ── Login qua Chrome (Playwright persistent context) ──────────────

def _setup_bundled_playwright() -> None:
    """Khi frozen: trỏ Playwright driver + chromium về bản đã nhúng trong _internal."""
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    browsers = meipass / "_ms-playwright"
    driver = meipass / "playwright" / "driver"
    if browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    if driver.exists():
        os.environ["PLAYWRIGHT_DRIVER_PATH"] = str(driver)


def _profile_dir(slot: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", slot or "default")
    p = app_dir() / "tiktok_profiles" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def login_blocking(slot: str = "", timeout_sec: int = 300) -> str | None:
    """Mở Trình duyệt (Edge/Chrome), chờ user đăng nhập, quét cookie sessionid rồi lưu.

    Mỗi `slot` là một profile riêng → đăng nhập được nhiều tài khoản.
    Chạy đồng bộ (blocking) — gọi trong thread riêng.
    """
    from playwright.sync_api import sync_playwright

    if getattr(sys, "frozen", False):
        _setup_bundled_playwright()

    user_data_dir = str(_profile_dir(slot or f"acc{len(list_accounts()) + 1}"))
    with sync_playwright() as p:
        # Thử mở qua Microsoft Edge có sẵn trên Windows, fallback về Chrome / default
        ctx = None
        for channel in ("msedge", "chrome", None):
            try:
                launch_kwargs = {
                    "user_data_dir": user_data_dir,
                    "headless": False,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if channel:
                    launch_kwargs["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**launch_kwargs)
                break
            except Exception as e:
                logger.debug("Playwright launch with channel=%s failed: %s", channel, e)
        if ctx is None:
            raise RuntimeError("Không thể mở trình duyệt Edge hoặc Chrome để đăng nhập.")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.tiktok.com/login")
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    cookies = ctx.cookies(["https://www.tiktok.com"])
                except Exception:
                    break  # user đóng cửa sổ
                for c in cookies:
                    if c.get("name") == "sessionid" and c.get("value"):
                        sess = str(c["value"])
                        save_account(sess, name=slot)
                        logger.info("TikTok login OK: %s...", sess[:8])
                        return sess
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return None


# ── Text splitting ───────────────────────────────────────────────

def _fits(s: str) -> bool:
    return len(s) <= MAX_TEXT_CHARS and len(s.encode("utf-8")) <= MAX_TEXT_BYTES


def split_text(text: str) -> list[str]:
    """Cắt text theo cả 2 giới hạn (ký tự + byte), ưu tiên ngắt ở khoảng trắng."""
    text = (text or "").strip()
    if not text:
        return []
    if _fits(text):
        return [text]

    parts: list[str] = []
    cur = ""
    for token in text.split(" "):
        cand = f"{cur} {token}".strip()
        if _fits(cand):
            cur = cand
            continue
        if cur:
            parts.append(cur)
            cur = ""
        if _fits(token):
            cur = token
        else:
            parts.extend(_split_token(token))
    if cur:
        parts.append(cur)
    return parts


def _split_token(token: str) -> list[str]:
    out, cur = [], ""
    for ch in token:
        if _fits(cur + ch):
            cur += ch
        else:
            if cur:
                out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


# ── Engine ───────────────────────────────────────────────────────

async def _request_part(client: httpx.AsyncClient, text: str, voice_id: str) -> bytes:
    ids = _session_ids()
    attempts = max(len(ids), 3)
    last_err: Exception | None = None
    for attempt in range(attempts):
        session_id = _next_session(attempt)
        if not session_id:
            raise RuntimeError("Chưa đăng nhập TikTok (không có sessionid)")
        endpoint = API_ENDPOINTS[attempt % len(API_ENDPOINTS)]
        try:
            resp = await client.post(
                endpoint,
                params={
                    "text_speaker": voice_id,
                    "req_text": text,
                    "speaker_map_type": 0,
                    "aid": 1233,
                },
                headers={
                    "User-Agent": USER_AGENT,
                    "Cookie": f"sessionid={session_id}",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code == 429:
                raise TikTokRateLimited("HTTP 429")
            resp.raise_for_status()
            data = resp.json()
            msg = str(data.get("message") or data.get("status_msg") or "")
            if msg.lower() not in ("success", ""):
                if any(k in msg.lower() for k in ("rate limit", "too many", "blocked")):
                    raise TikTokRateLimited(msg)
                raise RuntimeError(f"TikTok TTS: {msg}")
            v_str = (data.get("data") or {}).get("v_str")
            if not v_str:
                raise RuntimeError("TikTok TTS: thiếu v_str trong response")
            return base64.b64decode(v_str)
        except TikTokRateLimited as exc:
            _COOLDOWN[session_id] = time.monotonic() + COOLDOWN_SEC
            last_err = exc
            logger.warning("TikTok session %s... bị rate limit, nghỉ %.0fs", session_id[:6], COOLDOWN_SEC)
        except Exception as exc:
            last_err = exc
            logger.warning("TikTok TTS attempt %d thất bại: %s", attempt + 1, exc)
        await asyncio.sleep(0.5)
    raise RuntimeError(f"TikTok TTS thất bại sau {attempts} lần: {last_err}")


async def generate(text: str, voice_id: str, output_path: str) -> str:
    """Sinh mp3 TikTok TTS vào output_path; nối các phần bằng ghép byte thô."""
    parts = split_text(text)
    if not parts:
        raise ValueError("TikTok TTS: text rỗng")
    async with httpx.AsyncClient(timeout=15) as client:
        chunks = [await _request_part(client, p, voice_id) for p in parts]
    with open(output_path, "wb") as f:
        for c in chunks:
            f.write(c)
    if os.path.getsize(output_path) < 500:
        raise RuntimeError("TikTok TTS: file audio quá nhỏ / hỏng")

    # Tự động tăng tốc x1.1 cho TikTok native audio
    try:
        from pydub import AudioSegment
        from pydub.effects import speedup
        from core.config import settings
        AudioSegment.converter = settings.get("ffmpeg_path", "ffmpeg")
        seg = AudioSegment.from_file(output_path)
        if len(seg) > 200:
            fast_seg = speedup(seg, playback_speed=1.1)
            fast_seg.export(output_path, format="mp3")
    except Exception as exc:
        logger.warning("TikTok TTS x1.1 speedup skipped: %s", exc)

    return output_path


if __name__ == "__main__":
    # ponytail: self-check cho phần thuần logic (split), không đụng mạng.
    assert split_text("") == []
    assert split_text("xin chao") == ["xin chao"]
    long_vi = "Xin chào các bạn, đây là một câu tiếng Việt rất dài có dấu. " * 10
    parts = split_text(long_vi)
    assert len(parts) > 1
    assert all(_fits(p) for p in parts), "mọi phần phải nằm trong giới hạn"
    assert "".join(p.replace(" ", "") for p in parts) == long_vi.strip().replace(" ", "")
    blob = "à" * 500  # 1 token dài, 2 byte/ký tự
    assert all(_fits(p) for p in split_text(blob))
    print("OK")
