# Sever Dub Studio Pro

Ứng dụng desktop tự động thuyết minh video: tải video → nhận dạng giọng nói → dịch → lồng tiếng → render phụ đề → xuất video hoàn chỉnh. Chạy hoàn toàn trên máy, không cần server ngoài.

## Tính năng

- **8 bước pipeline**: Download → Watermark/Sub detect → Audio chunk → STT → Translate → TTS → Overlay → Concat.
- **STT local**: FunASR Paraformer + FSMN-VAD + dấu câu, tự tải model về `%APPDATA%/SeverDesktop/models_cache`.
- **Dịch**: Gemini 3.5 Flash Lite qua Gemini web (guest miễn phí); thêm tài khoản Google trong Settings để tăng hạn mức.
- **TTS local**: Piper `ngoc_huyen` (offline, ~0.2–0.6 s/câu) + 2 giọng CapCut (TikTok) dùng session TikTok.
- **Phụ đề**: ASS căn giữa box blur, viền đậm chuẩn CapCut/TikTok, canvas theo đúng khung hình.
- **Encode**: tự dò NVENC / QSV / AMF, fallback `libx264`; concat không re-encode, tự fallback khi cần.
- **Giao diện**: pywebview (WebView2) hoặc trình duyệt, theme Fluent Windows 11.

## Yêu cầu

- Python 3.11+
- FFmpeg / ffprobe / yt-dlp / BBDown (bản build full đã kèm sẵn trong `bin/`)

## Cài đặt

```bash
pip install -r requirements.txt
```

## Chạy

```bash
python run.py          # mở cửa sổ desktop
```

Hoặc chạy riêng backend để mở bằng trình duyệt:

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 15577
# http://127.0.0.1:15577
```

## Build

- `BUILD_LITE.bat` — bản nhẹ, không kèm model AI.
- `BUILD_FULL.bat` — bản offline đầy đủ: kèm FunASR models, Piper voice, binaries, đóng gói bằng Inno Setup.

## Cấu trúc

```
sever_desktop/
├── run.py                 # Entry point: backend thread + pywebview
├── app.py                 # FastAPI server + REST API
├── core/                  # config, SQLite jobs, ffmpeg, auth (tiktok/google/bilibili)
├── pipeline/              # step01..step08 + orchestrator
├── providers/             # gemini_stealth
├── src/                   # UI web (index.html, app.js, styles.css, icons/)
├── models/piper/          # voice ONNX (không commit)
├── hooks/                 # runtime hook PyInstaller
├── installer.iss          # Inno Setup
└── SeverDubStudioPro.spec # PyInstaller
```

## Ghi chú

- Settings lưu tại `%APPDATA%/SeverDesktop/settings.json`; job DB tại `jobs.db`.
- Model AI và binaries tự tải lần đầu nếu bản cài không kèm.
- Tài khoản (Bilibili / CapCut / Google) thêm trong **Settings**; cookie/token lưu cục bộ, không gửi đi đâu khác.
