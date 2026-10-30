"""Đăng nhập Google (Gemini) đa tài khoản: mở trình duyệt, chờ user đăng nhập,
tự quét cookie + token SNlM0e rồi đóng cửa sổ. Lưu trong settings (`google_accounts`)."""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

from core.config import app_dir, settings

logger = logging.getLogger(__name__)

GOOGLE_COOKIES = (
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSID",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
)


def list_accounts() -> list[dict]:
    return list(settings.get("google_accounts", []) or [])


def public_list() -> list[dict]:
    """Danh sách rút gọn cho UI, không trả cookie/token."""
    out = []
    for a in list_accounts():
        ident = str(a.get("id", ""))
        out.append({
            "id": ident,
            "name": a.get("name") or "Google",
            "preview": (ident[:10] + "…") if ident else "",
            "added_at": a.get("added_at", ""),
            "has_token": bool(a.get("token")),
        })
    return out


def save_account(cookies: list[dict], token: str = "", name: str = "") -> dict:
    sid = next((str(c.get("value")) for c in cookies if c.get("name") == "__Secure-1PSID"), "")
    if not sid:
        raise ValueError("Không thấy cookie __Secure-1PSID")

    accounts = list_accounts()
    rec = next((a for a in accounts if a.get("id") == sid), None)
    if rec is None:
        rec = {"id": sid}
        accounts.append(rec)
    rec.update({
        "name": name or rec.get("name") or f"Google {len(accounts)}",
        "token": token,
        "cookies": [
            {"name": c.get("name"), "value": c.get("value"), "domain": c.get("domain"), "path": c.get("path")}
            for c in cookies if c.get("name") in GOOGLE_COOKIES
        ],
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    settings.set("google_accounts", accounts)
    return rec


def delete_account(account_id: str) -> None:
    settings.set("google_accounts", [a for a in list_accounts() if a.get("id") != account_id])


def _profile_dir() -> Path:
    p = app_dir() / "google_profiles" / f"acc{len(list_accounts()) + 1}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _setup_bundled_playwright() -> None:
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    browsers = meipass / "_ms-playwright"
    driver = meipass / "playwright" / "driver"
    if browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    if driver.exists():
        os.environ["PLAYWRIGHT_DRIVER_PATH"] = str(driver)


def _extract_token(page) -> str:
    try:
        token = page.evaluate("() => (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || ''")
        if token:
            return str(token)
    except Exception:
        pass
    try:
        html = page.content()
    except Exception:
        return ""
    match = re.search(r'SNlM0e\\?"?\s*[:=]\s*\\?"([A-Za-z0-9_\-]{10,})', html)
    return match.group(1) if match else ""


def login_blocking(timeout_sec: int = 300) -> str | None:
    """Mở Edge/Chrome tới Gemini, chờ đăng nhập, lưu cookie+token, đóng browser."""
    from playwright.sync_api import sync_playwright

    if getattr(sys, "frozen", False):
        _setup_bundled_playwright()

    user_data_dir = str(_profile_dir())
    with sync_playwright() as p:
        ctx = None
        for channel in ("msedge", "chrome", None):
            try:
                kwargs = {
                    "user_data_dir": user_data_dir,
                    "headless": False,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if channel:
                    kwargs["channel"] = channel
                ctx = p.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as exc:
                logger.debug("Playwright launch channel=%s failed: %s", channel, exc)
        if ctx is None:
            raise RuntimeError("Không mở được Edge/Chrome để đăng nhập Google.")

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    cookies = ctx.cookies(["https://gemini.google.com", "https://www.google.com"])
                except Exception:
                    break
                if any(c.get("name") == "__Secure-1PSID" and c.get("value") for c in cookies):
                    token = _extract_token(page)
                    rec = save_account(cookies, token)
                    logger.info("Google login OK: %s…", str(rec.get("id", ""))[:8])
                    return str(rec.get("id", "")) or None
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return None
