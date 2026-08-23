"""Download + auto title extraction + translate title."""
import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ponytail: hide console windows on Windows
_CREATION_FLAGS = 0x08000000 if sys.platform == "win32" else 0

from core.config import settings

logger = logging.getLogger(__name__)


def detect_source(url: str) -> str:
    url_lower = url.lower()
    if "bilibili" in url_lower or "b23.tv" in url_lower:
        return "bilibili"
    return "youtube"


async def download_video(url: str, output_dir: str, job_id: str) -> dict[str, str]:
    """Download video, return {video_path, title, source_url}."""
    os.makedirs(output_dir, exist_ok=True)
    source = detect_source(url)

    if source == "bilibili":
        # Tier: BBDown -> yt-dlp -> direct API (khong can cookie)
        try:
            res = await _download_bbdown(url, output_dir, job_id)
        except Exception as exc:
            logger.warning("BBDown failed (%s), trying yt-dlp...", exc)
            try:
                res = await _download_ytdlp(url, output_dir, job_id)
            except Exception as exc2:
                logger.warning("yt-dlp failed (%s), trying direct API...", exc2)
                res = await _download_bilibili_direct(url, output_dir, job_id)
    else:
        res = await _download_ytdlp(url, output_dir, job_id)

    # Tự động giảm tốc 0.8x ngầm để kéo dãn timeline cho STT và TTS không bị đè/lấn giọng
    res["video_path"] = await _apply_speed(res["video_path"], 0.8)
    return res


async def _apply_speed(video_path: str, speed: float) -> str:
    """Dieu chinh toc do video theo speed factor (0.5-1.5).

    speed < 1.0 = cham hon, speed > 1.0 = nhanh hon, speed == 1.0 = khong doi.
    """
    if not os.path.exists(video_path):
        return video_path
    if speed <= 0 or speed == 1.0:
        return video_path

    dir_name, file_name = os.path.split(video_path)
    base_name, ext = os.path.splitext(file_name)
    speed_tag = f"_speed{str(speed).replace('.', '')}"
    if speed_tag in base_name:
        return video_path

    speed_path = os.path.join(dir_name, f"{base_name}{speed_tag}{ext}")
    ffmpeg_bin = find_executable(settings.get("ffmpeg_path", "ffmpeg")) or "ffmpeg"

    # ponytail: try video+audio first, if no audio stream → video-only
    cmd = [
        ffmpeg_bin, "-y",
        "-i", video_path,
        "-filter_complex", f"[0:v]setpts=PTS/{speed}[v];[0:a]atempo={speed}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
        speed_path
    ]
    logger.info("Applying x%.2f speed adjustment to video: %s -> %s", speed, video_path, speed_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
        if proc.returncode == 0 and os.path.exists(speed_path):
            return speed_path
        # ponytail: audio stream missing — retry video-only
        err_msg = stderr.decode("utf-8", errors="replace")[-500:] if stderr else ""
        if "does not contain any stream" in err_msg or "Audio stream not found" in err_msg:
            logger.warning("No audio stream in source, applying speed video-only")
            cmd_video_only = [
                ffmpeg_bin, "-y",
                "-i", video_path,
                "-filter_complex", f"[0:v]setpts=PTS/{speed}[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-an",
                speed_path
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd_video_only, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATION_FLAGS,
            )
            await asyncio.wait_for(proc2.communicate(), timeout=3600)
            if proc2.returncode == 0 and os.path.exists(speed_path):
                return speed_path
    except Exception as exc:
        logger.warning("Failed to apply speed %.2f adjustment: %s, fallback to original video", speed, exc)
    return video_path


import shutil


def find_executable(cmd_name: str) -> str | None:
    if not cmd_name:
        return None
    p = Path(cmd_name)
    if p.exists() and p.is_file():
        return str(p)
    found = shutil.which(cmd_name)
    if found:
        return found
    if os.name == "nt" and not cmd_name.lower().endswith(".exe"):
        found = shutil.which(f"{cmd_name}.exe")
        if found:
            return found
    # ponytail: fallback to bundled binaries in PyInstaller _internal/bin/
    from core.config import _bundled_bin
    bare = Path(cmd_name).stem.lower()
    bundled = _bundled_bin(bare)
    if bundled:
        return bundled
    return None


async def _fetch_title_ytdlp(url: str) -> str:
    """Get video title before download (supports redirect resolution, Bilibili API direct + yt-dlp metadata JSON)."""
    import re
    import urllib.request
    import json

    # 0. Resolve short links (b23.tv, v.douyin.com, vm.tiktok.com)
    if any(s in url for s in ("b23.tv", "v.douyin.com", "vm.tiktok.com")):
        try:
            def _resolve_redirect():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.geturl()
            resolved = await asyncio.to_thread(_resolve_redirect)
            if resolved:
                url = resolved
        except Exception:
            pass

    # 1. Bilibili direct API check (nhanh và lấy đúng 100% tiêu đề chính của phim/video)
    if "bilibili.com" in url or "b23.tv" in url:
        m = re.search(r'(BV[a-zA-Z0-9]+)', url)
        if m:
            bvid = m.group(1)
            try:
                def _bili_api():
                    req = urllib.request.Request(
                        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    )
                    with urllib.request.urlopen(req, timeout=6) as r:
                        data = json.loads(r.read().decode("utf-8"))
                        return data.get("data", {}).get("title", "")
                title = await asyncio.to_thread(_bili_api)
                if title:
                    return title.strip()
            except Exception as exc:
                logger.debug("Bilibili title API fail: %s", exc)

    # 2. yt-dlp JSON metadata extraction cho mọi nền tảng
    ytdlp_bin = find_executable(settings.get("ytdlp_path", "yt-dlp")) or "yt-dlp"
    cmd = [
        ytdlp_bin,
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0 and stdout:
            info = json.loads(stdout.decode("utf-8", errors="replace"))
            title = info.get("title") or info.get("fulltitle") or info.get("playlist_title") or ""
            if title:
                # Bỏ đuôi tập ' p01 1' nếu có
                title = re.sub(r'\s+p\d+\s+\d+$', '', title, flags=re.IGNORECASE).strip()
                return title
    except Exception as exc:
        logger.debug("yt-dlp json title fetch fail: %s", exc)

    return ""


async def _download_ytdlp(url: str, output_dir: str, job_id: str) -> dict[str, str]:
    """yt-dlp: lấy title trước, download, trả về path + title."""
    ytdlp_bin = find_executable(settings.get("ytdlp_path", "yt-dlp"))
    if not ytdlp_bin:
        raise RuntimeError(
            "Khong tim thay yt-dlp. Cai dat tu yt-dlp.github.io "
            "roi cau hinh duong dan trong Settings hoac them vao PATH."
        )

    title = await _fetch_title_ytdlp(url)
    logger.info("download yt-dlp: title=%s", title or "(unknown)")

    tmpl = os.path.join(output_dir, f"{job_id}_%(id)s.%(ext)s")
    cmd = [
        ytdlp_bin,
        # ponytail: explicit format selector — always grab audio stream
        "-f", "bestvideo[height<=1080]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[height<=1080]/best",
        "--merge-output-format", "mp4",
        "-o", tmpl,
        "--no-playlist",
        "--print", "after_move:filepath",
        "--no-warnings",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except FileNotFoundError:
        raise RuntimeError(f"Không thể khởi chạy yt-dlp ({ytdlp_bin}). File không tồn tại.")

    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=7200)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"yt-dlp failed ({proc.returncode}): {err}")
    path = stdout.decode("utf-8", errors="replace").strip().splitlines()[-1].strip()
    if not os.path.isfile(path):
        mp4s = sorted(Path(output_dir).glob("*.mp4"), key=os.path.getmtime, reverse=True)
        path = str(mp4s[0]) if mp4s else path

    return {"video_path": path, "title": title, "source_url": url}


async def _download_bbdown(url: str, output_dir: str, job_id: str) -> dict[str, str]:
    """BBDown + title từ response (Nếu không có BBDown, tự động fallback sang yt-dlp)."""
    bbdown_cfg = settings.get("bbdown_path", "BBDown")
    bbdown_bin = find_executable(bbdown_cfg)

    if not bbdown_bin:
        raise RuntimeError(f"BBDown executable not found ({bbdown_cfg}). Vui lòng cài BBDown (app sẽ tự dùng yt-dlp/direct API).")

    cookie_file = settings.get("bbdown_cookie", "")
    args = [bbdown_bin, url, "--work-dir", output_dir]
    if cookie_file and Path(cookie_file).exists():
        args += ["-c", cookie_file]

    # Kiểm tra xem URL có chọn tập cụ thể không (vd ?p=2)
    p_match = re.search(r"[?&]p=(\d+)", url)
    if p_match:
        args += ["-p", p_match.group(1)]
    else:
        args += ["-p", "1"]

    ffmpeg_bin = find_executable(settings.get("ffmpeg_path", "ffmpeg"))
    if ffmpeg_bin:
        args += ["--ffmpeg-path", ffmpeg_bin]

    args += ["-F", f"{job_id}"]
    args += ["--skip-cover", "--skip-subtitle", "-tv", "-mt"]

    logger.info("download BBDown: %s", url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except FileNotFoundError:
        raise RuntimeError(f"Khong the khoi chay BBDown ({bbdown_bin}). File khong ton tai.")
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=7200)
    output = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")[-2000:]

    if proc.returncode != 0:
        raise RuntimeError(f"BBDown failed ({proc.returncode}): {err}")

    # Parse title from BBDown output: thường in "Title: ..." hoặc dòng đầu
    title = await _fetch_title_ytdlp(url)
    if not title:
        for line in output.splitlines():
            line = line.strip()
            if line.lower().startswith("title:"):
                title = line.split(":", 1)[1].strip()
                break
        if not title:
            for line in output.splitlines():
                line = line.strip()
                if line and not line.startswith("[") and not line.startswith("-") and not line.lower().startswith("bbdown") and not line.lower().startswith("video bilibili"):
                    title = line
                    break
    if title.lower() in ("video bilibili", "bilibili_video", "bilibili video", "bilibili"):
        title = ""

    # Tìm file video (kể cả trong thư mục con do BBDown tự tạo theo tập)
    for f in sorted(Path(output_dir).rglob("*"), key=os.path.getmtime, reverse=True):
        if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".flv"):
            return {"video_path": str(f.resolve()), "title": title, "source_url": url}
    raise FileNotFoundError(f"BBDown: no video found in {output_dir}")


async def _download_bilibili_direct(url: str, output_dir: str, job_id: str) -> dict[str, str]:
    """Tai truc tiep video Bilibili thong qua API + FFmpeg (khong can cookie).

    Ho tro:
    - Video thuong: BV... -> x/web-interface/view + x/player/wbi/playurl (wbi signature)
    - Bangumi (phim ban quyen): /bangumi/play/ep... hoac ss...
      -> pgc/view/web/season + pgc/player/web/playurl (khong cookie, tranh 412 cua yt-dlp)
    """
    import urllib.request
    import urllib.parse
    import ssl
    import json

    os.makedirs(output_dir, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # 1. Xac dinh loai URL: bangumi (ep/ss) hay video thuong (BV)
    ep_match = re.search(r"/bangumi/play/ep(\d+)", url)
    ss_match = re.search(r"/bangumi/play/ss(\d+)", url)
    bvid_match = re.search(r"(BV[a-zA-Z0-9]+)", url)

    title = ""
    video_stream_url = ""
    audio_stream_url = ""

    if ep_match or ss_match:
        # Bangumi: khong can wbi, khong can cookie
        if ep_match:
            ep_id = ep_match.group(1)
            view_api_url = f"https://api.bilibili.com/pgc/view/web/season?ep_id={ep_id}"
        else:
            season_id = ss_match.group(1)
            view_api_url = f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}"
        req = urllib.request.Request(view_api_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            view_data = json.loads(resp.read().decode("utf-8"))
        if view_data.get("code") != 0:
            raise RuntimeError(f"Bilibili bangumi view API error: {view_data.get('message')}")
        result = view_data.get("result") or {}
        season_title = result.get("title", "")
        episodes = result.get("episodes", []) or []
        if ep_match:
            ep = next((e for e in episodes if str(e.get("id")) == ep_id), None)
        else:
            ep = episodes[0] if episodes else None
        if not ep:
            raise RuntimeError("Bilibili bangumi: khong tim thay tap trong season")
        title = ep.get("title") or season_title
        target_ep_id = str(ep.get("id") or ep_id)

        play_params = {"ep_id": target_ep_id, "qn": "80", "fnval": "16", "fourk": "1"}
        play_api_url = f"https://api.bilibili.com/pgc/player/web/playurl?{urllib.parse.urlencode(play_params)}"
        req_play = urllib.request.Request(play_api_url, headers=headers)
        with urllib.request.urlopen(req_play, context=ctx, timeout=15) as resp_play:
            play_data = json.loads(resp_play.read().decode("utf-8"))
        if play_data.get("code") != 0:
            raise RuntimeError(f"Bilibili bangumi playurl API error: {play_data.get('message')}")
        data = play_data.get("result") or play_data.get("data") or {}
    else:
        # Video thuong: BV -> view + wbi playurl
        if not bvid_match:
            raise ValueError(f"Cannot find BV id from url: {url}")
        bvid = bvid_match.group(1)
        img_key, sub_key = await _get_bilibili_wbi_keys(headers, ctx)

        view_api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        req = urllib.request.Request(view_api_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            view_data = json.loads(resp.read().decode("utf-8"))
        if view_data.get("code") != 0 or "data" not in view_data:
            raise RuntimeError(f"Bilibili view API error: {view_data.get('message')}")
        title = view_data["data"].get("title", "")
        cid = view_data["data"].get("cid")
        if not cid:
            raise RuntimeError("Bilibili view API: missing cid")

        play_params = {"bvid": bvid, "cid": str(cid), "qn": "80", "fnval": "16", "fourk": "1"}
        signed_params = _sign_wbi_params(play_params, img_key, sub_key)
        play_api_url = f"https://api.bilibili.com/x/player/wbi/playurl?{urllib.parse.urlencode(signed_params)}"
        req_play = urllib.request.Request(play_api_url, headers=headers)
        with urllib.request.urlopen(req_play, context=ctx, timeout=15) as resp_play:
            play_data = json.loads(resp_play.read().decode("utf-8"))
        if play_data.get("code") != 0:
            raise RuntimeError(f"Bilibili playurl API error: {play_data.get('message')}")
        data = play_data.get("data", {})

    # 2. Chon stream quality cao nhat
    if "dash" in data:
        dash = data["dash"]
        if dash.get("video"):
            video_streams = sorted(dash["video"], key=lambda x: x.get("bandwidth", 0), reverse=True)
            video_stream_url = video_streams[0].get("baseUrl") or video_streams[0].get("base_url", "")
        if dash.get("audio"):
            audio_streams = sorted(dash["audio"], key=lambda x: x.get("bandwidth", 0), reverse=True)
            audio_stream_url = audio_streams[0].get("baseUrl") or audio_streams[0].get("base_url", "")
    elif "durl" in data and data["durl"]:
        video_stream_url = data["durl"][0].get("url", "")

    if not video_stream_url:
        raise RuntimeError("Bilibili API: khong tim thay stream video URL")

    # 3. Tai stream bang urllib (SSL ong hoa) sau do ghep bang FFmpeg
    ffmpeg_bin = find_executable(settings.get("ffmpeg_path", "ffmpeg")) or "ffmpeg"
    bvid_tag = (bvid_match.group(1) if bvid_match else
                (ep_match.group(1) if ep_match else (ss_match.group(1) if ss_match else "bangumi")))
    output_path = os.path.join(output_dir, f"{job_id}_{bvid_tag}.mp4")

    download_headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://www.bilibili.com",
    }

    video_tmp = os.path.join(output_dir, f"{job_id}_{bvid_tag}_video.m4s")
    audio_tmp = os.path.join(output_dir, f"{job_id}_{bvid_tag}_audio.m4s")

    try:
        # Tai video stream
        logger.info("Downloading video stream...")
        req_v = urllib.request.Request(video_stream_url, headers=download_headers)
        with urllib.request.urlopen(req_v, context=ctx, timeout=300) as resp_v:
            with open(video_tmp, "wb") as f:
                while True:
                    chunk = resp_v.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        logger.info("Video stream downloaded: %s", video_tmp)

        # Tai audio stream
        if audio_stream_url:
            logger.info("Downloading audio stream...")
            req_a = urllib.request.Request(audio_stream_url, headers=download_headers)
            with urllib.request.urlopen(req_a, context=ctx, timeout=300) as resp_a:
                with open(audio_tmp, "wb") as f:
                    while True:
                        chunk = resp_a.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            logger.info("Audio stream downloaded: %s", audio_tmp)

        # Ghep video + audio bang FFmpeg
        cmd = [ffmpeg_bin, "-y", "-i", video_tmp]
        if audio_stream_url and os.path.exists(audio_tmp):
            cmd.extend(["-i", audio_tmp, "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", output_path])
        else:
            cmd.extend(["-c:v", "copy", "-movflags", "+faststart", output_path])

        logger.info("Merging streams: %s", output_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0 or not os.path.exists(output_path):
            err_msg = stderr.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"FFmpeg merge failed ({proc.returncode}): {err_msg}")
    finally:
        # Xoa file tam
        for tmp in [video_tmp, audio_tmp]:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    return {"video_path": output_path, "title": title, "source_url": url}


# ── Bilibili WBI Signature ──────────────────────────────────────

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_WBI_KEYS_CACHE: dict[str, str] = {}


def _get_mixin_key(raw: str) -> str:
    return functools.reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _sign_wbi_params(params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
    """Sign Bilibili API params with wbi signature."""
    mixin_key = _get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())
    params["wts"] = str(curr_time)

    # Sort and filter params
    params = dict(sorted(params.items()))
    # Remove special characters
    query = urllib.parse.urlencode(params)
    wbi_sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    params["w_rid"] = wbi_sign
    return params


async def _get_bilibili_wbi_keys(headers: dict, ctx) -> tuple[str, str]:
    """Get img_key and sub_key from Bilibili nav API for wbi signature."""
    global _WBI_KEYS_CACHE
    if _WBI_KEYS_CACHE.get("img_key") and _WBI_KEYS_CACHE.get("sub_key"):
        return _WBI_KEYS_CACHE["img_key"], _WBI_KEYS_CACHE["sub_key"]

    try:
        nav_url = "https://api.bilibili.com/x/web-interface/nav"
        req = urllib.request.Request(nav_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            nav_data = json.loads(resp.read().decode("utf-8"))

        wbi_img = nav_data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        img_key = img_url.split("/")[-1].split(".")[0] if img_url else ""
        sub_key = sub_url.split("/")[-1].split(".")[0] if sub_url else ""

        if img_key and sub_key:
            _WBI_KEYS_CACHE["img_key"] = img_key
            _WBI_KEYS_CACHE["sub_key"] = sub_key
            logger.info("Bilibili wbi keys fetched successfully")
            return img_key, sub_key
    except Exception as exc:
        logger.warning("Failed to fetch Bilibili wbi keys: %s", exc)

    raise RuntimeError("Không thể lấy Bilibili wbi keys. Vui lòng thử lại sau.")
