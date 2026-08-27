# CHANGELOG — Sever Dub Studio Pro

Mỗi bản build mới sẽ được ghi lại tại đây theo thứ tự mới nhất lên trên.

---

## v0.2.0 — 2026-08-25

**Build:** `SeverDubStudioPro.exe` — `dist/SeverDubStudioPro/`
**Status:** Released

### Changes

#### Fix: Step 02 Watermark NameError
- Khắc phục lỗi `NameError: per_frame is not defined` trong `_filter_target_regions` khi video không nhận diện được subtitle band ở lần lặp đầu.

#### Feat & Fix: TTS 2-Tier Retry Logic & Exclusively TikTok Local + Server
- Cấu hình TTS chạy độc quyền 2 nguồn: TikTok Local (`core.tiktok`) và TikTok Server (`https://tts.maika.sbs`).
- Mỗi câu thoại retry 2 lần. Khi toàn bộ batch hoàn tất, thực hiện pass retry thứ 2 cho danh sách các câu bị fail để đảm bảo đầy đủ giọng đọc.

#### Fix: Database SQLite Video Path Update
- Cập nhật đúng `video_path` vào SQLite sau khi xuất file hoàn tất ở Step 08 Concat và dọn dẹp thư mục tạm.

#### Fix: Step 04 STT Error Propagation
- Ném lỗi `RuntimeError` rõ ràng khi cả FunASR và STT fallback đều hỏng, tránh nuốt lỗi âm thầm và xuất video câm.

#### Fix: Step 01 Download Audio Recovery
- Quét lại file gốc trong thư mục tải về để phục hồi audio stream nếu video sau speed bị mất tiếng.

---

## v0.1.0 — 2026-08-13

**Build:** `SeverDubStudioPro.exe` — `dist/SeverDubStudioPro/`
**Status:** Released

### Changes

#### Fix: Subtitle chồng chất / chồng lấn (Image 1 — video đầu tiên)
- **Nguyên nhân:** `step04_stt.py` — `merge_fragmented_segments()` ghép `token_timestamps` bằng concat thô mà không interpolate gap → `_split_by_token_timestamps` tạo segment có timestamp trùng nhau → ASS render nhiều dialogue lines cùng lúc.
- **Fix:** Shift `token_timestamps` của segment sau bằng gap time trước khi merge (`step04_stt.py:363-375`).

#### Fix: Segment không hiện sub (Image 2 — video đầu tiên)
- **Nguyên nhân:** `step07_overlay.py` — `_build_ass()` trừ `chunk_start` nhầm từ segment timestamps. Nhưng STT chạy trên chunk audio → timestamps đã là chunk-relative → double-subtract → segment bị clamp sai vị trí, một số biến mất.
- **Fix:** Bỏ hoàn toàn `chunk_start` parameter khỏi `_build_ass()` và `_plan_tts_timing()`, dùng trực tiếp segment start/end đã chunk-relative.
- **Thêm:** `_resolve_overlaps()` — sort theo start time, clip end của segment trước nếu overlap với segment sau, tránh ASS render chồng.

#### Fix: Box blur FFmpeg `Invalid chroma_param radius` (2 lần lỗi)
- **Nguyên nhân:** `chroma_radius` và `luma_radius` cố định (12/9) — vượt giới hạn khi box blur quá ngắn (chroma plane = nửa kích thước, `radius must be < chroma_plane_min_dim`).
- **Fix:** Tính radius động theo kích thước box: `blur_r = max(1, min(12, min(w_crop, h_crop) // 4))`. An toàn mọi kích thước.

#### Tăng font subtitle + Thu hẹp box blur
- **Font:** `20 * 1.8` → `20 * 2.5 = 50px` (tăng 50% cho rõ trên 720p)
- **Box blur padding:** `pad_x=12, pad_y=6` → `pad_x=1, pad_y=1` (phủ bì 1px mỗi bên)
- **Box blur phát hiện:** Y expansion `0.65/0.75` → `0.35/0.45` (chiều cao box blur giảm ~40%)

#### Phụ
- Thêm `__version__ = "0.1.0"` vào `core/__init__.py`
- Tạo `CHANGELOG.md`

### Known Issues
- Box blur detection đôi khi lệch nếu sub gốc có hiệu ứng gradient trong suốt
