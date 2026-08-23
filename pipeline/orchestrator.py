"""Pipeline orchestrator — runs steps 1-8 sequentially with progress callbacks."""
import asyncio
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from core.config import settings
from core.db import create_job, update_job, get_job

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int, str, float], Awaitable[None]]
"""args: (step_index, step_name, progress_0_1)"""


class PipelineOrchestrator:
    """Coordinate all 8 steps for one job."""

    def __init__(self, source_url: str, source_lang: str = "zh",
                 target_lang: str = "vi", voice: str = "",
                 title: str = "", remove_sub: bool = True,
                 job_id: str = "",
                 on_progress: ProgressCb | None = None,
                 cancel_event: threading.Event | None = None):
        self.source_url = source_url
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.voice = voice
        self.title = title
        self.remove_sub = remove_sub
        self.job_id = job_id
        self.on_progress = on_progress
        # ponytail: threading.Event used cross-thread from UI → pipeline; asyncio.Event not thread-safe
        self.cancel_event = cancel_event or threading.Event()

        self._steps: list[dict] = []
        self._chunks: list[dict] = []
        self._regions: list[dict] = []
        self._rendered_chunks: list[str] = []
        self._tts_files: list[str] = []

    async def run(self) -> dict[str, Any]:
        """Execute full pipeline."""
        if not self.job_id:
            self.job_id = create_job(self.source_url, self.title,
                                      self.source_lang, self.target_lang)
        update_job(self.job_id, status="running")

        self._steps = [
            (1, "download", self._step_download, 0.05),
            (2, "watermark", self._step_watermark, 0.10),
            (3, "audio_chunk", self._step_chunk, 0.15),
            (4, "stt", self._step_stt, 0.35),
            (5, "translate", self._step_translate, 0.50),
            (6, "tts", self._step_tts, 0.70),
            (7, "overlay", self._step_overlay, 0.85),
            (8, "concat", self._step_concat, 1.00),
        ]

        try:
            for idx, name, fn, weight in self._steps:
                if self.cancel_event.is_set():
                    update_job(self.job_id, status="cancelled")
                    return {"job_id": self.job_id, "status": "cancelled"}
                update_job(self.job_id, current_stage=name)
                await self._progress(idx, name, 0)
                await fn()
                await self._progress(idx, name, 1)

            update_job(self.job_id, status="done", progress=1.0)
            job = get_job(self.job_id)
            logger.info("pipeline done: %s", self.job_id)
            return job or {"job_id": self.job_id, "status": "done"}

        except asyncio.CancelledError:
            update_job(self.job_id, status="cancelled")
            raise
        except Exception as exc:
            logger.exception("pipeline failed: %s", exc)
            update_job(self.job_id, status="failed", error=str(exc))
            raise
        finally:
            self._cleanup_all_temp_files()

    async def _progress(self, step_index: int, name: str, sub_progress: float) -> None:
        if self.on_progress:
            await self.on_progress(step_index, name, sub_progress)

    def _cleanup_all_temp_files(self) -> None:
        """Dọn sạch mọi rác tạm (TTS, folder download, chunks, overlay intermediate). Chỉ giữ file video xuất cuối."""
        # 1. File TTS tạm
        for pth in self._tts_files:
            try:
                os.remove(pth)
            except OSError:
                pass
        self._tts_files.clear()

        # 2. Xóa folder download tạm của job
        if hasattr(self, "_download_dir") and self._download_dir and os.path.exists(self._download_dir):
            try:
                shutil.rmtree(self._download_dir, ignore_errors=True)
                logger.info("cleaned download dir: %s", self._download_dir)
            except Exception as e:
                logger.warning("failed to remove download dir: %s", e)

        # 3. Kết thúc job: chuyển file video kết quả ra thẳng output_dir gốc, xóa thư mục con job_id
        out_base = settings.get("output_dir", "")
        job_dir = os.path.join(out_base, self.job_id)
        if os.path.exists(job_dir) and job_dir != out_base:
            mp4s = [f for f in os.listdir(job_dir) if f.endswith(".mp4") and "_ch" not in f and "_overlay" not in f]
            for mp4 in mp4s:
                src_p = os.path.join(job_dir, mp4)
                dst_p = os.path.join(out_base, mp4)
                try:
                    if os.path.exists(dst_p):
                        os.remove(dst_p)
                    shutil.move(src_p, dst_p)
                    update_job(self.job_id, output_dir=out_base)
                except Exception as e:
                    logger.warning("failed to move final mp4 to base output dir: %s", e)
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass

    # ── Step 1: download ──────────────────────────────────

    async def _step_download(self) -> None:
        import re
        from pipeline.step01_download import download_video, _fetch_title_ytdlp
        output_dir = os.path.join(settings.get("download_dir", ""), self.job_id)
        self._download_dir = output_dir

        # Kiểm tra xem title hiện tại có phải là placeholder tạm thời hay không
        def _is_placeholder_title(t: str) -> bool:
            if not t or not t.strip():
                return True
            t_low = t.strip().lower()
            if t_low in ("video bilibili", "bilibili_video", "bilibili video", "bilibili", "video thuyết minh", "video"):
                return True
            if re.match(r"^video\s+(bv[a-za-z0-9]+|thuyết minh)$", t_low):
                return True
            return False

        # 1. BƯỚC TIỀN XỬ LÝ: TỰ LẤY & DỊCH TIÊU ĐỀ NẾU CHƯA CÓ HOẶC ĐANG LÀ PLACEHOLDER TẠM
        if _is_placeholder_title(self.title):
            try:
                raw_title = await _fetch_title_ytdlp(self.source_url)
                if raw_title:
                    from pipeline.step05_translate import translate_tier
                    tiers = settings.get_translate_tiers()
                    if tiers and self.source_lang != self.target_lang:
                        translated = await translate_tier([raw_title], tiers[0], self.source_lang, self.target_lang)
                        if translated and translated[0]:
                            self.title = translated[0].strip()
                        else:
                            self.title = raw_title.strip()
                    else:
                        self.title = raw_title.strip()
                    update_job(self.job_id, title=self.title)
                    logger.info("Pre-download title fetched & translated: %s", self.title)
            except Exception as exc:
                logger.warning("Pre-download title fetch failed: %s", exc)

        # 2. TIẾN HÀNH TẢI VIDEO
        result = await download_video(self.source_url, output_dir, self.job_id)
        video_path = result["video_path"]
        dl_title = result.get("title", "")

        # ponytail: verify video has audio stream after download + speed
        from core.ffmpeg import has_audio_stream
        if not await has_audio_stream(video_path):
            logger.warning("Downloaded video has NO audio stream: %s", video_path)
            # Try re-download without speed to see if speed filter dropped audio
            from pipeline.step01_download import find_executable
            ffmpeg_bin = find_executable(settings.get("ffmpeg_path", "ffmpeg")) or "ffmpeg"
            # Attempt: strip speed, keep original audio
            raw_path = video_path.replace("_speed", "_raw")
            if not os.path.exists(raw_path):
                import asyncio as _aio
                proc = await _aio.create_subprocess_exec(
                    ffmpeg_bin, "-y", "-i", video_path,
                    "-c:v", "copy", "-c:a", "copy", raw_path,
                    stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
                    creationflags=0x08000000,
                )
                await _aio.wait_for(proc.communicate(), timeout=60)
                if proc.returncode == 0 and await has_audio_stream(raw_path):
                    logger.info("Re-extracted audio from speed-processed video")
                    video_path = raw_path

        # Nếu title vẫn là placeholder mà downloader lấy được dl_title thì dịch và cập nhật
        if _is_placeholder_title(self.title) and dl_title and not _is_placeholder_title(dl_title):
            try:
                from pipeline.step05_translate import translate_tier
                tiers = settings.get_translate_tiers()
                if tiers and self.source_lang != self.target_lang:
                    translated = await translate_tier([dl_title], tiers[0], self.source_lang, self.target_lang)
                    if translated and translated[0]:
                        self.title = translated[0].strip()
                    else:
                        self.title = dl_title.strip()
                else:
                    self.title = dl_title.strip()
            except Exception:
                self.title = dl_title.strip()

        final_title = self.title if not _is_placeholder_title(self.title) else "Video Thuyết Minh"
        update_job(self.job_id, video_path=video_path, title=final_title)
        self.title = final_title
        logger.info("downloaded: %s title=%s", video_path, final_title)

    # ── Step 2: watermark ─────────────────────────────────

    async def _step_watermark(self) -> None:
        if not self.remove_sub:
            self._regions = []
            return
        job = get_job(self.job_id)
        if not job or not job.get("video_path"):
            raise RuntimeError("no video to detect watermark")
        from pipeline.step02_watermark import detect_blur_regions
        self._regions = await detect_blur_regions(job["video_path"])
        # Lưu regions vào job settings mà không ghi đè mất cấu hình riêng (sub_style, voice, ...)
        cur_settings = job.get("settings_json") or {}
        if isinstance(cur_settings, str):
            try:
                cur_settings = json.loads(cur_settings)
            except Exception:
                cur_settings = {}
        cur_settings["watermark_blur_regions"] = self._regions
        update_job(self.job_id, settings_json=json.dumps(cur_settings, ensure_ascii=False))
        logger.info("watermark: %d regions", len(self._regions))

    # ── Step 3: audio_chunk ───────────────────────────────

    async def _step_chunk(self) -> None:
        job = get_job(self.job_id)
        if not job or not job.get("video_path"):
            raise RuntimeError("no video to chunk")
        from pipeline.step03_audio_chunk import split_into_chunks
        output_dir = os.path.join(settings.get("download_dir", ""), self.job_id, "chunks")
        self._chunks = await split_into_chunks(job["video_path"], output_dir, self.job_id)
        update_job(self.job_id, chunks=len(self._chunks))
        logger.info("audio_chunk: %d chunks", len(self._chunks))

    # ── Step 4: STT ───────────────────────────────────────

    # ── Step 4: STT ───────────────────────────────────────

    async def _step_stt(self) -> None:
        from pipeline.step04_stt import run_stt
        n = len(self._chunks)
        cpu = os.cpu_count() or 2
        # STT trên CPU: Chạy 2-3 worker song song tùy theo số lõi CPU để xử lý nhanh gấp 2-3 lần
        stt_workers = max(1, min(3, max(1, cpu // 3)))
        sem = asyncio.Semaphore(stt_workers)
        completed = 0

        async def _stt_one(i: int, ch: dict):
            nonlocal completed
            if self.cancel_event.is_set():
                return
            async with sem:
                segments = await run_stt(ch["audio_path"], self.source_lang)
                self._chunks[i]["segments"] = segments
                completed += 1
                await self._progress(4, "stt", completed / max(n, 1))
                logger.info("stt chunk %d/%d: %d segments", i + 1, n, len(segments))

        tasks = [_stt_one(i, ch) for i, ch in enumerate(self._chunks)]
        await asyncio.gather(*tasks)

    # ── Step 5: translate ─────────────────────────────────

    async def _step_translate(self) -> None:
        from pipeline.step05_translate import run_translate
        n = len(self._chunks)
        # Context chain: 20 segment da dich giu lai, day qua tung chunk de
        # xung ho/ten nhan vat nhat quan xuyen ca video (chuan sever_v2)
        history: list[dict] = []
        title_context = f"Title: {self.title or ''}"
        for i, ch in enumerate(self._chunks):
            if self.cancel_event.is_set():
                return
            if ch["segments"]:
                translated = await run_translate(
                    ch["segments"], self.source_lang, self.target_lang,
                    history=history, context=title_context)
                self._chunks[i]["segments"] = translated
                history.extend([
                    {"index": j, "text": sg.get("text", ""),
                     "translated": sg.get("translated_text", "")}
                    for j, sg in enumerate(translated)
                ])
                history = history[-20:]
            await self._progress(5, "translate", (i + 1) / n)
            logger.info("translate chunk %d/%d", i + 1, n)

    # ── Step 6: TTS ───────────────────────────────────────

    async def _step_tts(self) -> None:
        from pipeline.step06_tts import run_tts
        total = sum(len(ch["segments"]) for ch in self._chunks)
        done = 0
        # 6 worker song song cho TTS để tăng tốc độ I/O qua mạng
        sem = asyncio.Semaphore(6)

        async def _process_seg(ci, si, text):
            nonlocal done
            if self.cancel_event.is_set():
                return
            async with sem:
                tts_path = await run_tts(text, self.voice)
                self._chunks[ci]["segments"][si]["tts_path"] = tts_path
                self._tts_files.append(tts_path)
                done += 1
                await self._progress(6, "tts", done / max(total, 1))

        tasks = []
        for ci, ch in enumerate(self._chunks):
            for si, seg in enumerate(ch["segments"]):
                text = seg.get("translated_text") or seg.get("text", "")
                if text.strip():
                    tasks.append(_process_seg(ci, si, text))
                else:
                    done += 1

        if tasks:
            await asyncio.gather(*tasks)

    # ── Step 7: overlay ───────────────────────────────────

    async def _step_overlay(self) -> None:
        job = get_job(self.job_id)
        if not job or not job.get("video_path"):
            raise RuntimeError("no video to overlay")
        from pipeline.step07_overlay import overlay_chunk
        output_dir = os.path.join(settings.get("output_dir", ""), self.job_id)
        os.makedirs(output_dir, exist_ok=True)
        n = len(self._chunks)
        # Chunk độc lập → render song song tận dụng đa lõi / GPU
        cpu = os.cpu_count() or 2
        overlay_workers = min(4, max(2, cpu // 2))
        sem = asyncio.Semaphore(overlay_workers)

        async def _render(i: int, ch: dict) -> str | None:
            if self.cancel_event.is_set():
                return None
            async with sem:
                rendered = await overlay_chunk(
                    job["video_path"], ch, self._regions,
                    ch["segments"], output_dir, self.job_id, self.target_lang)
            await self._progress(7, "overlay", (i + 1) / n)
            logger.info("overlay chunk %d/%d: %s", i + 1, n, rendered)
            return rendered

        results = await asyncio.gather(*[_render(i, ch) for i, ch in enumerate(self._chunks)])
        self._rendered_chunks = [r for r in results if r]
        if len(self._rendered_chunks) != n:
            failed = n - len(self._rendered_chunks)
            raise RuntimeError(f'overlay failed: {failed}/{n} chunks failed to render')

    # ── Step 8: concat ────────────────────────────────────

    async def _step_concat(self) -> None:
        if not self._rendered_chunks:
            raise RuntimeError("no rendered chunks to concat")
        from pipeline.step08_concat import final_concat
        output_dir = os.path.join(settings.get("output_dir", ""), self.job_id)
        result = await final_concat(
            self._rendered_chunks, output_dir, self.job_id,
            self.title, self.target_lang)
        update_job(self.job_id, output_dir=output_dir)
        logger.info("concat done: %s", result.get("output_path", ""))
