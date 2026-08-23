"""FFmpeg helpers — mirror logic from sever/core/ffmpeg.py."""
import asyncio
import json
import logging
import os
import sys
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

# ponytail: hide console windows on Windows when spawning ffmpeg/ffprobe
_CREATION_FLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

_BEST_ENCODER: list[str] | None = None


def get_best_video_encoder() -> list[str]:
    """Tự động phát hiện và trả về cờ encoder tối ưu nhất cho GPU/CPU (NVENC > QSV > AMF > CPU libx264)."""
    global _BEST_ENCODER
    if _BEST_ENCODER is not None:
        return list(_BEST_ENCODER)

    import subprocess
    candidates = [
        ("NVIDIA NVENC", ["-c:v", "h264_nvenc", "-preset", "p4"]),
        ("Intel QSV", ["-c:v", "h264_qsv", "-preset", "fast"]),
        ("AMD AMF", ["-c:v", "h264_amf", "-quality", "speed"]),
    ]
    ffmpeg_bin = settings.get("ffmpeg_path", "ffmpeg")
    for name, args in candidates:
        try:
            cmd = [ffmpeg_bin, "-hide_banner", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04"] + args + ["-f", "null", "-"]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, creationflags=_CREATION_FLAGS)
            if res.returncode == 0:
                logger.info("Auto-detected GPU hardware encoder: %s (%s)", name, args[1])
                _BEST_ENCODER = args
                return list(_BEST_ENCODER)
        except Exception:
            pass

    logger.info("Using standard CPU encoder: libx264 (preset fast)")
    _BEST_ENCODER = ["-c:v", "libx264", "-preset", "fast"]
    return list(_BEST_ENCODER)


async def _run(cmd: list[str], timeout: int = 7200) -> str:
    logger.debug("ffmpeg: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_CREATION_FLAGS,
        )
    except FileNotFoundError:
        tool_name = cmd[0] if cmd else "FFmpeg"
        raise FileNotFoundError(
            f"Khong tim thay '{tool_name}' trong PATH. "
            f"Cai dat tu ffmpeg.org/yt-dlp.github.io roi cau hinh duong dan trong Settings."
        ) from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"ffmpeg timed out after {timeout}s: {' '.join(cmd)}") from None
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {err}")
    return stdout.decode("utf-8", errors="replace")


async def has_audio_stream(path: str) -> bool:
    """Check if video file contains an audio stream."""
    out = await _run([
        settings.get("ffprobe_path", "ffprobe"),
        "-v", "quiet", "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", path,
    ])
    return bool(out.strip())


async def get_duration(path: str) -> float:
    out = await _run([
        settings.get("ffprobe_path", "ffprobe"),
        "-v", "quiet", "-show_entries", "format=duration",
        "-of", "json", path,
    ])
    return float(json.loads(out).get("format", {}).get("duration", 0))


async def get_dimensions(path: str) -> tuple[int, int]:
    out = await _run([
        settings.get("ffprobe_path", "ffprobe"),
        "-v", "quiet", "-show_entries", "stream=width,height",
        "-of", "json", path,
    ])
    streams = json.loads(out).get("streams", [])
    for s in streams:
        w, h = s.get("width", 0), s.get("height", 0)
        if w and h:
            return w, h
    return 0, 0


async def detect_scenes(path: str, threshold: float = 0.3) -> list[float]:
    """Return scene-change timestamps (seconds)."""
    ffmpeg_bin = settings.get("ffmpeg_path", "ffmpeg")
    cmd = [
        ffmpeg_bin,
        "-i", path, "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr", "-f", "null", "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_CREATION_FLAGS,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    out = stderr.decode("utf-8", errors="replace")
    times: list[float] = []
    for line in out.splitlines():
        if "pts_time:" in line:
            try:
                pts = line.split("pts_time:")[1].split()[0]
                times.append(float(pts))
            except (IndexError, ValueError):
                pass
    return times


async def extract_audio(path: str, output: str, start: float = 0, duration: float | None = None) -> None:
    cmd = [
        settings.get("ffmpeg_path", "ffmpeg"),
        "-y", "-ss", f"{start:.2f}",
        "-i", path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
    ]
    if duration:
        cmd.extend(["-t", f"{duration:.2f}"])
    cmd.append(output)
    await _run(cmd, timeout=600)


async def blur_region(input_path: str, output_path: str, regions: list[dict[str, Any]],
                      sub_text: str = "", sub_config: dict | None = None) -> None:
    """Apply boxblur on regions + optional ASS subtitle overlay."""
    ffmpeg = settings.get("ffmpeg_path", "ffmpeg")
    filter_chains: list[str] = []

    if regions:
        # Build blur chain: split → crop region → gblur → overlay back
        chain_parts = [f"[0:v]split={len(regions)+1}[vbase{'|'.join(f'[v{i}]' for i in range(len(regions)))}"]
        # Actually simpler: one pass per region
        filter_parts = []
        for i, r in enumerate(regions):
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            sigma = r.get("sigma", 10)
            filter_parts.append(
                f"[0:v]crop={w}:{h}:{x}:{y},gblur=sigma={sigma}:steps=3[blur{i}];"
                f"[0:v][blur{i}]overlay={x}:{y}[v{i}]"
            )
            if i < len(regions) - 1:
                filter_parts.append(f"[v{i}]")  # won't work cleanly in simple chain

    cmd = [ffmpeg, "-y", "-i", input_path]
    vf_parts: list[str] = []

    if regions:
        curr_v = "[0:v]"
        for i, r in enumerate(regions):
            x, y, w, h = int(r["x"]), int(r["y"]), int(r.get("w", 0)), int(r.get("h", 0))
            sigma = int(r.get("sigma", 12))
            next_v = f"[vblur{i}]"
            vf_parts.append(
                f"{curr_v}split[vmain{i}][vcrop{i}];"
                f"[vcrop{i}]crop={w}:{h}:{x}:{y},gblur=sigma={sigma}:steps=3[b{i}];"
                f"[vmain{i}][b{i}]overlay={x}:{y}{next_v}"
            )
            curr_v = next_v
        cmd.extend(["-filter_complex", ";".join(vf_parts), "-map", curr_v])
    else:
        cmd.extend(["-c:v", "copy"])

    cmd.extend(["-c:a", "copy", output_path])
    await _run(cmd, timeout=3600)


async def concat_files(file_list: list[str], output_path: str) -> None:
    """Concat video chunks via concat demuxer."""
    import tempfile
    clean_paths = [os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") for p in file_list]
    lines = "\n".join(f"file '{p}'" for p in clean_paths)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(lines + "\n")
        list_path = f.name
    try:
        cmd = [
            settings.get("ffmpeg_path", "ffmpeg"),
            "-y", "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy", output_path,
        ]
        await _run(cmd, timeout=3600)
    finally:
        import os as _os
        if _os.path.exists(list_path):
            _os.unlink(list_path)
