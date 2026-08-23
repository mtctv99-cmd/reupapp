# AGENTS.md

Workspace guide for Sever Dub Studio Pro (desktop media dubbing & translation pipeline).

## Repository Overview
Desktop GUI app automating video subtitle translation and audio dubbing locally.
- **Frontend / UI**: Web-based (`src/index.html`, `src/app.js`, `src/styles.css`) embedded via `pywebview` window (or browser fallback).
- **Backend / API**: FastAPI server running on `http://127.0.0.1:15577` (`app.py`), managed by background thread in launcher `run.py`.
- **Pipeline (`pipeline/`)**: 8-step modular video processing orchestrated by `pipeline/orchestrator.py`.
- **Core Helpers (`core/`)**: Config (`core/config.py`), SQLite jobs store (`core/db.py`), FFmpeg wrappers (`core/ffmpeg.py`), platform downloaders/auth (`core/tiktok.py`, `core/bilibili_auth.py`).
- **Binary Dependencies (`bin/`)**: Bundled `ffmpeg.exe`, `ffprobe.exe`, `yt-dlp.exe`, `BBDown.exe`.

## 8-Step Pipeline
1. `pipeline/step01_download.py`: Video download via `yt-dlp` / `BBDown`.
2. `pipeline/step02_watermark.py`: Subtitle / watermark region detection using `RapidOCR` + temporal voting.
3. `pipeline/step03_audio_chunk.py`: Split video into chunks & extract audio.
4. `pipeline/step04_stt.py`: Speech-to-text with token timestamps (`FunASR`/`Paraformer` or Whisper).
5. `pipeline/step05_translate.py`: Translation via Google Translate / Gemini Stealth / Custom API.
6. `pipeline/step06_tts.py`: Audio synthesis via `edge-tts` or TTS server.
7. `pipeline/step07_overlay.py`: Old sub blur + ASS subtitle overlay + speed-adjusted TTS audio mix.
8. `pipeline/step08_concat.py`: Merge chunk outputs into final video via FFmpeg concat.

## Commands
- **Run Application**: `python run.py` (or execute `CHAY_APP.bat`)
- **Run All Tests**: `pytest -v` (includes Unit, API, Core, and Playwright E2E tests)
- **Run Unit Tests**: `python -m unittest tests_check.py` or `pytest test_overlay_timing.py test_core_and_pipeline.py`
- **Run API Tests**: `pytest test_api.py`
- **Run E2E UI Tests**: `pytest test_e2e.py`
- **Run Standalone Pipeline Test**: `python test_pipeline.py`
- **Build Windows Executable**: `BUILD.bat` (runs `pyinstaller SeverDubStudioPro.spec --clean --noconfirm`)

## Key Architecture & Coding Constraints
- **Timing & Subtitles**:
  - Segment timestamps in `step07_overlay.py` are chunk-relative. Do not subtract `chunk_start` again.
  - Box blur dimensions must dynamically cap radii: `blur_r = max(1, min(12, min(w_crop, h_crop) // 4))` to prevent FFmpeg chroma radius crashes.
- **Paths & Packaging**:
  - Binaries are resolved via `core.config.get_bin_dir()`, which checks `_internal/bin` when packaged or `bin/` in development.
  - Use `core.config.app_dir()` to access the user configuration directory (`%APPDATA%/SeverDesktop` on Windows).
- **Windows / Event Loop Gotchas**:
  - `run.py` includes monkey-patches for ProactorEventLoop `WinError 10054` and kills stale processes holding port `15577`.
  - When running frozen EXE without console, stdio is redirected to prevent crashes.
