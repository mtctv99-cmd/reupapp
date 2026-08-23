# Cách gửi Sever Desktop cho người khác

## Option 1: Gửi source + build script (recommended)

Gửi toàn bộ thư mục `sever_desktop/`, người nhận chạy `build.bat`.

Hướng dẫn nhanh:
1. Mở **Command Prompt (CMD)**
2. `cd` vào thư mục
3. Gõ `build.bat`
4. Đợi build xong, file `.exe` ở `dist\SeverDesktop.exe`

> Yêu cầu: Python 3.10+ (nhắc họ cài từ python.org, check "Add to PATH")

## Option 2: Gửi .exe + kèm FFmpeg/yt-dlp

Khi người nhận không rành kỹ thuật:

**Bước 1 — Build** (máy bạn):
```batch
build.bat
```
file `dist\SeverDesktop.exe` ~30MB.

**Bước 2 — Gói kèm tools**:
Tải 3 file, ghi vào cùng thư mục hoặc hướng dẫn họ tải:
- `ffmpeg.exe` + `ffprobe.exe` từ https://ffmpeg.org/download.html
- `yt-dlp.exe` từ https://github.com/yt-dlp/yt-dlp/releases
- (Nếu cần Bilibili) `BBDown.exe` từ https://github.com/nilaoda/BBDown/releases

**Bước 3 — Co kích thước** (nếu file quá to):

Thêm `--exclude torch --exclude funasr` trong `build.bat` để bỏ STT local.
Người dùng sẽ cần API key OpenAI để dùng Whisper API.

## Option 3: Git release

Sau khi build, upload file `.exe` lên GitHub Releases. 
Code gốc người ta có thể clone và build lại.

---

## File gửi kèm (SeverDesktop_V1_Release.zip)

Nội dung:
```
SeverDesktop.exe          — App chính (không cần cài Python)
ffmpeg.exe / ffprobe.exe  — FFmpeg (tách rời, thêm vào PATH)
yt-dlp.exe                — Downloader
BBDown.exe                — Bilibili download
```

Tổng ~120MB sau khi zip.
