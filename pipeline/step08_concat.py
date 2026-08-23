"""Step 8: concat finished chunks + optional publish / manifest."""
import json
import logging
import os
from typing import Any

from core.ffmpeg import concat_files, _run as ffmpeg_run
from core.config import settings

logger = logging.getLogger(__name__)


async def final_concat(chunk_paths: list[str], output_dir: str, job_id: str,
                       title: str = "", target_lang: str = "vi") -> dict[str, Any]:
    """Concat all rendered chunks into final video.
    Returns {video_path, duration, size, output_dir}.
    """
    import re
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() if title else ""
    if safe_title and safe_title.lower() not in ("video bilibili", "video"):
        output_filename = f"{safe_title}_{job_id[:8]}_{target_lang}.mp4"
    else:
        output_filename = f"{job_id}_{target_lang}.mp4"
    output_path = os.path.join(output_dir, output_filename)

    logger.info("concat: %d chunks → %s", len(chunk_paths), output_path)
    await concat_files(chunk_paths, output_path)

    # Áp dụng tốc độ xuất cuối cùng từ thanh trượt UI (video_speed)
    try:
        final_speed = float(settings.get("video_speed", 1.0))
    except (TypeError, ValueError):
        final_speed = 1.0

    if final_speed and abs(final_speed - 1.0) > 0.01:
        from pipeline.step01_download import _apply_speed
        logger.info("Applying final output speed adjustment x%.2f to %s", final_speed, output_path)
        output_path = await _apply_speed(output_path, final_speed)

    # Probe result
    from core.ffmpeg import get_duration
    duration = await get_duration(output_path)
    size = os.path.getsize(output_path)

    return {
        "job_id": job_id,
        "title": title,
        "target_language": target_lang,
        "duration_s": round(duration, 2),
        "size_bytes": size,
        "output_path": output_path,
        "chunks": len(chunk_paths),
    }
