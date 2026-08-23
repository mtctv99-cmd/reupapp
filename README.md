# Sever Desktop

Desktop app cho media pipeline — kế thừa logic từ sever_v2 backend, đơn giản hoá chạy local.

## Pipeline 8 bước

| # | Bước | Mô tả | Công cụ |
|---|------|-------|---------|
| 1 | **Download** | Tải video từ YouTube / Bilibili | yt-dlp / BBDown |
| 2 | **Watermark** | Detect vùng có sub cứng / logo cần blur | RapidOCR + temporal voting |
| 3 | **Audio chunk** | Split video → chunks, extract audio | FFmpeg scene detect |
| 4 | **STT** | Speech-to-text từng chunk | FunASR/Paraformer hoặc OpenAI Whisper API |
| 5 | **Translate** | Dịch segment text | Google Translate (free) hoặc sever server |
| 6 | **TTS** | Text-to-speech từng segment | edge-tts (free, local) hoặc sever TTS server |
| 7 | **Overlay** | Blur sub cũ + overlay TTS + sub mới lên chunk | FFmpeg + ASS |
| 8 | **Concat** | Ghép chunks thành video hoàn chỉnh | FFmpeg concat |

## Cài đặt

```bash
pip install -r requirements.txt

# Optional: thêm FunASR cho STT local
pip install funasr modelscope

# FFmpeg + yt-dlp + BBDown (cài riêng)
# Windows: choco install ffmpeg yt-dlp
# hoặc download từ ffmpeg.org, yt-dlp.github.io
```

## Chạy

```bash
python ui/app.py
```

## Cấu trúc

```
sever_desktop/
├── core/
│   ├── config.py      # Settings manager (JSON)
│   ├── db.py          # SQLite job store
│   └── ffmpeg.py      # FFmpeg helpers
├── pipeline/
│   ├── step01_download.py
│   ├── step02_watermark.py
│   ├── step03_audio_chunk.py
│   ├── step04_stt.py
│   ├── step05_translate.py
│   ├── step06_tts.py
│   ├── step07_overlay.py
│   ├── step08_concat.py
│   └── orchestrator.py   # Pipeline runner
├── ui/
│   └── app.py            # CustomTkinter desktop UI
├── providers/             # Future: provider plugins
├── requirements.txt
└── README.md
```

## Settings

- **STT providers**: FunASR (local), OpenAI Whisper API
- **TTS providers**: edge-tts (local, free), sever TTS server
- **Translate providers**: Google (free API), sever translate server
- **Paths**: ffmpeg, ffprobe, yt-dlp, BBDown paths
- **General**: source/target language, subtitle font, remove_sub toggle
