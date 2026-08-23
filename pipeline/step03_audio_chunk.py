"""Step 3: audio chunk — silence-based splitting theo V2 reference.

Logic:
- Extract full audio from video
- Detect silence using pydub (dBFS-based)
- Split into chunks at silence boundaries
- Each chunk gets its own audio file for STT
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from core.config import settings
from core.ffmpeg import get_duration, extract_audio

logger = logging.getLogger(__name__)

# ── Config (theo V2) ─────────────────────────────────────

_SILENCE_THRESH_OFFSET_DB = 16
_MIN_SILENCE_LEN_MS = 500
_CHUNK_MAX_SECONDS = 300.0  # 5 min max
_CHUNK_MIN_SECONDS = 30.0   # 30s min


@dataclass
class ChunkInfo:
    index: int
    start_ms: float
    end_ms: float
    audio_path: str = ""

    @property
    def start_seconds(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_seconds(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


async def split_into_chunks(
    video_path: str,
    output_dir: str,
    job_id: str,
) -> list[dict]:
    """Split video audio into chunks based on silence boundaries.
    ĐẢM BẢO LIÊN TỤC 100% TIMELINE VIDEO:
    Chunk[0].start = 0, Chunk[i].end == Chunk[i+1].start, Chunk[-1].end == duration.
    Không bao giờ cắt bỏ bất kỳ giây nào của video gốc.
    """
    os.makedirs(output_dir, exist_ok=True)
    duration_s = await get_duration(video_path)
    duration_ms = duration_s * 1000.0
    logger.info("audio_chunk: video duration=%.1fs", duration_s)

    if duration_s <= 0:
        return []

    # Extract full audio to temp wav
    full_audio_path = os.path.join(output_dir, f"{job_id}_full.wav")
    await extract_audio(video_path, full_audio_path)

    # Detect silence ranges
    silence_ranges = []
    try:
        from pydub import AudioSegment
        from pydub.silence import detect_silence
        audio = AudioSegment.from_file(full_audio_path)
        silence_thresh = audio.dBFS - _SILENCE_THRESH_OFFSET_DB if audio.dBFS != float("-inf") else -50
        silence_ranges = detect_silence(
            audio,
            min_silence_len=_MIN_SILENCE_LEN_MS,
            silence_thresh=silence_thresh,
        )
    except Exception as exc:
        logger.warning("pydub silence detection error: %s, using fixed chunking", exc)

    final_chunks = _split_continuous_timeline(duration_ms, silence_ranges)

    # Extract audio per chunk
    result = []
    for i, (s_ms, e_ms) in enumerate(final_chunks):
        audio_path = os.path.join(output_dir, f"{job_id}_ch{i:04d}.wav")
        start_s = s_ms / 1000.0
        dur_s = (e_ms - s_ms) / 1000.0

        # Extract audio chunk with ffmpeg
        await extract_audio(video_path, audio_path, start_s, dur_s)

        result.append({
            "chunk_index": i,
            "start": start_s,
            "end": e_ms / 1000.0,
            "audio_path": audio_path,
            "segments": [],
            "video_chunk_path": "",
        })

    logger.info("audio_chunk: split into %d continuous chunks covering full duration %.1fs", len(result), duration_s)
    return result


def _split_continuous_timeline(
    total_duration_ms: float,
    silence_ranges: list[list[int | float]],
    target_chunk_ms: float = 180000.0,
    min_chunk_ms: float = 30000.0,
    max_chunk_ms: float = 300000.0,
) -> list[tuple[float, float]]:
    """Chia timeline video thành các chunk liên tục từ 0 -> total_duration_ms.
    Ưu tiên cắt tại các khoảng lặng âm thanh (silence) để không làm đứt câu thoại.
    """
    if total_duration_ms <= max_chunk_ms:
        return [(0.0, total_duration_ms)]

    cut_points = [0.0]
    current = 0.0

    while current < total_duration_ms:
        target = current + target_chunk_ms
        if target >= total_duration_ms:
            break

        # Tìm khoảng lặng tốt nhất trong vùng [target - 60s, target + 60s]
        search_start = max(current + min_chunk_ms, target - 60000.0)
        search_end = min(total_duration_ms - min_chunk_ms, current + max_chunk_ms)

        best_cut = target
        best_silence_dur = -1.0

        for s_range in (silence_ranges or []):
            s_start, s_end = float(s_range[0]), float(s_range[1])
            if s_start >= search_start and s_end <= search_end:
                silence_dur = s_end - s_start
                if silence_dur > best_silence_dur:
                    best_silence_dur = silence_dur
                    best_cut = (s_start + s_end) / 2.0
            elif s_start < search_end and s_end > search_start:
                clipped_s = max(search_start, s_start)
                clipped_e = min(search_end, s_end)
                silence_dur = clipped_e - clipped_s
                if silence_dur > best_silence_dur:
                    best_silence_dur = silence_dur
                    best_cut = (clipped_s + clipped_e) / 2.0

        # Nếu đoạn còn lại quá ngắn (< min_chunk_ms), kết thúc luôn tại total_duration_ms
        if (total_duration_ms - best_cut) < min_chunk_ms:
            break

        cut_points.append(best_cut)
        current = best_cut

    cut_points.append(total_duration_ms)

    chunks = []
    for i in range(len(cut_points) - 1):
        s = cut_points[i]
        e = cut_points[i + 1]
        if e > s:
            chunks.append((s, e))

    return chunks


def _fixed_chunks(
    video_path: str,
    output_dir: str,
    job_id: str,
    duration_s: float,
) -> list[dict]:
    """Fallback: split into fixed-duration chunks covering 100% duration."""
    import math

    n = max(1, math.ceil(duration_s / _CHUNK_MAX_SECONDS))
    chunk_dur = duration_s / n

    chunks = []
    for i in range(n):
        s = i * chunk_dur
        e = duration_s if i == n - 1 else (i + 1) * chunk_dur
        chunks.append({
            "chunk_index": i,
            "start": s,
            "end": e,
            "audio_path": "",
            "segments": [],
            "video_chunk_path": "",
        })
    return chunks
