"""Step 7: overlay TTS audio + translated subtitles onto video chunk with blur."""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from core.ffmpeg import _run as ffmpeg_run
from core.config import settings

logger = logging.getLogger(__name__)


async def overlay_chunk(video_path: str, chunk: dict, regions: list[dict],
                        segments: list[dict], output_dir: str, job_id: str,
                        target_lang: str) -> str:
    """Render one chunk with blur regions + TTS audio + ASS subtitle.
    Returns path to rendered chunk.
    """
    from core.ffmpeg import get_dimensions

    ci = chunk["chunk_index"]
    os.makedirs(output_dir, exist_ok=True)
    duration = chunk["end"] - chunk["start"]

    # Get actual original dimensions of the source video
    orig_w, orig_h = await get_dimensions(video_path)
    if orig_w <= 0 or orig_h <= 0:
        orig_w, orig_h = 1920, 1080

    output = os.path.join(output_dir, f"{job_id}_ch{ci:04d}_overlay.mp4")
    ffmpeg = settings.get("ffmpeg_path", "ffmpeg")

    # ponytail: Find subtitle blur region to align the new ASS subtitle on top of it
    sub_region = next((r for r in (regions or []) if r.get("label") == "subtitle"), None)
    sub_blur_box = None
    if sub_region:
        sx, sy = int(sub_region.get("x", 0)), int(sub_region.get("y", 0))
        sw, sh = int(sub_region.get("w", 0)), int(sub_region.get("h", 0))
        if sw > 0 and sh > 0:
            target_w = min(orig_w, int(sw * 1.08))
            diff_w = target_w - sw
            sx = max(0, sx - diff_w // 2)
            sw = min(orig_w - sx, target_w)

            pad_x, pad_y = 3, 3
            x_crop = max(0, sx - pad_x)
            y_crop = max(0, sy - pad_y)
            w_crop = min(orig_w - x_crop, sw + 2 * pad_x)
            h_crop = min(orig_h - y_crop, sh + 2 * pad_y)

            x_crop = (x_crop // 2) * 2
            y_crop = (y_crop // 2) * 2
            w_crop = max(4, (w_crop // 2) * 2)
            h_crop = max(4, (h_crop // 2) * 2)

            sub_blur_box = {
                "x": x_crop,
                "y": y_crop,
                "w": w_crop,
                "h": h_crop,
                "center_x": round(x_crop + w_crop / 2.0),
                "center_y": round(y_crop + h_crop / 2.0),
                "label": "subtitle"
            }

    # Build single master TTS audio track via pydub kèm đồng bộ mốc thời gian thực tế cho ASS subtitle
    tts_master_path = os.path.join(output_dir, f"{job_id}_ch{ci:04d}_tts_mix.wav")
    tts_res, synced_segments = _build_chunk_tts_track(segments, duration, tts_master_path)
    has_tts = tts_res is not None

    # Build ASS subtitle file inside output_dir passing original dimensions & calculated blur box
    ass_path = _build_ass(synced_segments, duration, target_lang, output_dir, job_id, ci, sub_blur_box or sub_region, orig_w, orig_h)

    # 1. Inputs — cắt trực tiếp tại đây: -ss/-t kèm re-encode cho seek chính xác,
    # bỏ được một lần chạy ffmpeg extract mỗi chunk.
    cmd = [ffmpeg, "-y", "-ss", f"{chunk['start']:.3f}", "-t", f"{duration:.3f}", "-i", video_path]
    tts_input_idx = 0
    if has_tts:
        tts_input_idx = 1
        cmd.extend(["-i", tts_master_path])

    # ponytail: logo là input CUỐI để không đội index của TTS
    logo_path = str(settings.get("logo_path", "") or "")
    logo_idx = 0
    if logo_path and os.path.exists(logo_path):
        logo_idx = 2 if has_tts else 1
        cmd.extend(["-i", logo_path])

    fc_parts: list[str] = []
    v_chains = []

    curr_v = "[0:v]"
    video_map = "0:v"

    # B. BLUR on original coordinates (Bo góc mềm mại + viền mờ dần / feathering)
    if regions:
        for i, r in enumerate(regions):
            x, y, w, h = int(r["x"]), int(r["y"]), int(r.get("w", 0)), int(r.get("h", 0))
            if w <= 0 or h <= 0:
                continue

            # Mở rộng vùng box blur vừa vặn (giảm 10% so với trần cũ)
            if r.get("label") == "subtitle":
                target_w = min(orig_w, int(w * 1.08))
                diff_w = target_w - w
                x = max(0, x - diff_w // 2)
                w = min(orig_w - x, target_w)

            pad_x, pad_y = 3, 3
            x_crop = max(0, x - pad_x)
            y_crop = max(0, y - pad_y)
            w_crop = min(orig_w - x_crop, w + 2 * pad_x)
            h_crop = min(orig_h - y_crop, h + 2 * pad_y)

            # ponytail: Bắt buộc toạ độ và kích thước chẵn (even) để tránh lệch chroma YUV420p trong alphamerge
            x_crop = (x_crop // 2) * 2
            y_crop = (y_crop // 2) * 2
            w_crop = max(4, (w_crop // 2) * 2)
            h_crop = max(4, (h_crop // 2) * 2)

            # Tính bán kính bo góc tròn trịa (radius) & độ mờ mịn vừa vặn
            rad = max(8, min(28, h_crop // 2))
            blur_size = max(5, min(21, min(w_crop, h_crop) // 3))
            if blur_size % 2 == 0:
                blur_size += 1

            next_v = f"[vblur{i}]"
            # Tạo mask bo tròn góc (round rect) có feather mờ mịn tỏa về các cạnh rìa (gblur sigma=5, 3 steps)
            mask_expr = f"if(lte(hypot(X-min(max(X,{rad}),{w_crop}-{rad}),Y-min(max(Y,{rad}),{h_crop}-{rad})),{rad}),255,0)"
            v_chains.append(
                f"{curr_v}split[vmain{i}][vcrop{i}];"
                f"[vcrop{i}]crop={w_crop}:{h_crop}:{x_crop}:{y_crop},avgblur=sizeX={blur_size}:sizeY={blur_size}:planes=7[braw{i}];"
                f"color=c=black:s={w_crop}x{h_crop}:d=1,format=gray,geq=lum='{mask_expr}',gblur=sigma=5:steps=3[mask{i}];"
                f"[braw{i}][mask{i}]alphamerge[bround{i}];"
                f"[vmain{i}][bround{i}]overlay={x_crop}:{y_crop}{next_v}"
            )
            curr_v = next_v

    # C. SUBTITLES on original frame size, before any padding/scaling
    # Phụ đề Tiếng Việt được lồng cùng Subtitle Box đen mờ viền êm (\\blur2) phủ khớp vị trí sub gốc
    if ass_path and os.path.exists(ass_path):
        # ponytail: FFmpeg subtitles filter needs escaped paths on Windows
        # Use forward slashes + escape colons and backslashes/quotes for FFmpeg filter syntax
        clean_ass = ass_path.replace("\\", "/")
        clean_ass = clean_ass.replace("'", "'\\''")
        if len(clean_ass) > 2 and clean_ass[1] == ":":
            clean_ass = clean_ass[0] + "\\:" + clean_ass[2:]
        next_v = "[vsub]"
        v_chains.append(f"{curr_v}subtitles=filename=\\'{clean_ass}\\':original_size={orig_w}x{orig_h}{next_v}")
        curr_v = next_v

    # D. SCALE/PAD / BLUR BACKGROUND / CROP
    # Áp dụng khi user chọn tỉ lệ khác; mặc định 'original' giữ nguyên 100% video gốc
    aspect_ratio = str(settings.get("aspect_ratio", "original")).lower()
    bg_style = str(settings.get("bg_style", "blur")).lower()
    bg_color = str(settings.get("bg_color", "black")).lower()
    pad_color = "white" if "white" in bg_color else "black"

    target_w, target_h = 0, 0
    if "original" in aspect_ratio or not aspect_ratio:
        pass
    elif "3:4" in aspect_ratio or "3*4" in aspect_ratio:
        target_w, target_h = 1080, 1440
    elif "9:16" in aspect_ratio:
        target_w, target_h = 720, 1280
    elif "16:9" in aspect_ratio:
        target_w, target_h = 1280, 720

    if target_w > 0 and target_h > 0:
        zoom_out = "[vzoom]"
        is_source_9_16 = (orig_w > 0 and orig_h > 0 and abs((orig_w / orig_h) - (9 / 16)) < 0.08)
        if bg_style == "blur":
            # Nền mờ nghệ thuật chuyển động theo video: chuẩn hóa SAR=1, crop tâm, scale chẵn pixel
            v_chains.append(
                f"{curr_v}setsar=1,split[vbg_in][vfg_in];"
                f"[vbg_in]scale=w={target_w}:h={target_h}:force_original_aspect_ratio=increase,"
                f"crop=w={target_w}:h={target_h}:x=(in_w-{target_w})/2:y=(in_h-{target_h})/2,"
                f"boxblur=25:5,setsar=1[vbg_blur];"
                f"[vfg_in]scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[vfg_scaled];"
                f"[vbg_blur][vfg_scaled]overlay=x=(W-w)/2:y=(H-h)/2:format=auto{zoom_out}"
            )
            curr_v = zoom_out
        elif ("3:4" in aspect_ratio or "3*4" in aspect_ratio) and is_source_9_16:
            v_chains.append(f"{curr_v}scale={target_w}:-1,crop={target_w}:{target_h}:(iw-{target_w})/2:(ih-{target_h})/2,setsar=1{zoom_out}")
            curr_v = zoom_out
        else:
            # Màu nền cố định (Đen, Trắng, ...)
            scale_pad = (
                f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,"
                f"pad={target_w}:{target_h}:({target_w}-iw)/2:({target_h}-ih)/2:color={pad_color}"
            )
            v_chains.append(f"{curr_v}{scale_pad}{zoom_out}")
            curr_v = zoom_out

    # E. LOGO — dán sau scale/pad để vị trí tính theo khung cuối, không lệch vì letterbox
    if logo_idx:
        pos = str(settings.get("logo_position", "top_right"))
        try:
            opacity = max(0.0, min(1.0, float(settings.get("logo_opacity", 0.8))))
        except (TypeError, ValueError):
            opacity = 0.8
        m = 20  # ponytail: lề cố định 20px, chỉnh ở đây nếu cần theo % khung
        xy = {
            "top_left": f"{m}:{m}",
            "top_right": f"W-w-{m}:{m}",
            "bottom_left": f"{m}:H-h-{m}",
            "bottom_right": f"W-w-{m}:H-h-{m}",
        }.get(pos, f"W-w-{m}:{m}")
        next_v = "[vlogo]"
        v_chains.append(
            f"[{logo_idx}:v]scale=iw*min(1\\,120/iw):-1,format=rgba,"
            f"colorchannelmixer=aa={opacity:.2f}[lg];"
            f"{curr_v}[lg]overlay={xy}{next_v}"
        )
        curr_v = next_v

    if v_chains:
        fc_parts.append(";".join(v_chains))
        video_map = curr_v

    # Audio Filter Chain: Smart Ducking + Align TTS audio + Mix
    from core.ffmpeg import has_audio_stream
    src_has_audio = await has_audio_stream(video_path)

    # Điều chỉnh âm lượng video gốc theo cấu hình người dùng (0 -> 100%)
    try:
        orig_vol_val = float(settings.get("original_volume", 100)) / 100.0
    except (TypeError, ValueError):
        orig_vol_val = 1.0
    orig_vol_val = max(0.0, min(1.0, orig_vol_val))

    if has_tts:
        logger.info("overlay: mixing unified master TTS track with original audio")
        if src_has_audio:
            if orig_vol_val <= 0.001:
                # Mute hoàn toàn âm thanh gốc, chỉ lấy TTS track
                audio_map = f"{tts_input_idx}:a"
            else:
                # sever_v2 (0d4fb3b & 74bbb77): Giảm cố định -20dB (10% volume) toàn bộ video
                # Giữ nhạc nền và âm hiệu êm dịu xuyên suốt, triệt tiêu hoàn toàn tiếng thoại gốc Trung
                # không bị giật cục hay lộ tiếng gốc đè lên giữa các khoảng ngắt câu.
                bg_vol = max(0.01, orig_vol_val * 0.10)
                fc_parts.append(f"[{tts_input_idx}:a]aresample=44100[tts_resamp]")
                fc_parts.append(f"[0:a]volume={bg_vol:.3f},aresample=44100[bg_vol]")
                fc_parts.append("[bg_vol][tts_resamp]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.98:attack=5:release=50[aout]")
                audio_map = "[aout]"
        else:
            audio_map = f"{tts_input_idx}:a"
    else:
        if src_has_audio:
            if orig_vol_val <= 0.001:
                # Mute gốc khi không có TTS
                fc_parts.append("[0:a]volume=0.0[bg]")
                audio_map = "[bg]"
            elif abs(orig_vol_val - 1.0) > 0.01:
                fc_parts.append(f"[0:a]volume={orig_vol_val:.2f}[bg]")
                audio_map = "[bg]"
            else:
                audio_map = "0:a"
        else:
            audio_map = ""

    if fc_parts:
        cmd.extend(["-filter_complex", ";".join(fc_parts)])

    cmd.extend(["-map", video_map])
    if audio_map:
        cmd.extend(["-map", audio_map])
    elif src_has_audio:
        cmd.extend(["-map", "0:a"])

    from core.ffmpeg import get_best_video_encoder
    cmd.extend(get_best_video_encoder())
    cmd.extend(["-r", "30", "-pix_fmt", "yuv420p"])
    if audio_map or src_has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"])
    cmd.append(output)
    logger.info("overlay_chunk cmd: %s", " ".join(cmd))
    await ffmpeg_run(cmd, timeout=3600)
    if not os.path.exists(output):
        raise RuntimeError(f"FFmpeg failed to create overlay output file: {output}")
    return output


MAX_TEMPO = 1.35
"""Trần nén tốc độ TTS — vượt mức này giọng bắt đầu méo."""


def _build_chunk_tts_track(segments: list[dict], duration: float, output_path: str) -> tuple[str | None, list[dict]]:
    """Mix all TTS segments of a chunk into a single stereo WAV file with Ripple Pacing Shift.
    
    Đồng bộ 100% timeline giữa TTS audio và ASS subtitle:
    - Trả về (output_path, synced_segments) với mốc start/end thực tế khớp chuẩn xác thời điểm phát tiếng.
    - Tránh triệt để việc 2 câu thoại TTS đè giọng lên nhau khi câu trước đọc dài hơn slot.
    - Đẩy nhẹ điểm bắt đầu câu sau nếu câu trước chưa dứt (có gap 40ms).
    - Tự động co giãn tốc độ thông minh theo khoảng trống thực tế đến câu kế tiếp.
    """
    if not segments:
        return None, []
    try:
        from pydub import AudioSegment
        from pydub.effects import speedup
        AudioSegment.converter = settings.get("ffmpeg_path", "ffmpeg")

        track_ms = int(max(1.0, duration + 1.0) * 1000)
        master = AudioSegment.silent(duration=track_ms, frame_rate=44100).set_channels(2)
        has_audio = False

        # Thu thập các segment hợp lệ kèm index
        valid_entries = []
        for idx_orig, seg in enumerate(segments):
            p = seg.get("tts_path")
            s = max(0.0, float(seg.get("start", 0)))
            valid_entries.append((s, idx_orig, seg, p))
        valid_entries.sort(key=lambda x: x[0])

        cursor_ms = 0
        synced_map = {}

        for idx, (s, idx_orig, seg, p) in enumerate(valid_entries):
            if not p or not os.path.exists(p):
                synced_map[idx_orig] = dict(seg)
                continue
            try:
                audio = AudioSegment.from_file(p).set_frame_rate(44100).set_channels(2)
                # Boost nhẹ âm lượng TTS (+3.5 dB)
                audio = audio + 3.5

                # Điểm bắt đầu dự kiến
                planned_start_ms = int(s * 1000)
                # Ripple Shift: nếu câu trước đọc chưa xong, đẩy mốc bắt đầu ra sau câu trước 40ms
                actual_start_ms = max(planned_start_ms, cursor_ms + 40 if cursor_ms > 0 else planned_start_ms)

                # Khoảng trống khả dụng đến câu tiếp theo hoặc hết chunk
                if idx + 1 < len(valid_entries):
                    next_start_ms = int(valid_entries[idx + 1][0] * 1000)
                    available_slot_ms = max(200, next_start_ms - actual_start_ms)
                else:
                    available_slot_ms = max(200, int(duration * 1000) - actual_start_ms)

                cur_audio_len_ms = len(audio)
                if cur_audio_len_ms > available_slot_ms:
                    slot_sec = available_slot_ms / 1000.0
                    audio_sec = cur_audio_len_ms / 1000.0
                    tempo = min(MAX_TEMPO, audio_sec / max(0.05, slot_sec))
                    if tempo > 1.02 and cur_audio_len_ms > 150:
                        try:
                            audio = speedup(audio, playback_speed=tempo)
                        except Exception:
                            pass

                # Overlay vào master track
                master = master.overlay(audio, position=actual_start_ms)
                final_audio_len_ms = len(audio)
                cursor_ms = actual_start_ms + final_audio_len_ms
                has_audio = True

                # Cập nhật mốc thời gian thực tế khớp chuẩn từng mili-giây cho phụ đề ASS
                synced_seg = dict(seg)
                real_start_sec = round(actual_start_ms / 1000.0, 3)
                real_end_sec = round(cursor_ms / 1000.0, 3)
                synced_seg["start"] = real_start_sec
                synced_seg["end"] = real_end_sec
                synced_seg["t_start"] = real_start_sec
                synced_seg["t_end"] = real_end_sec
                synced_map[idx_orig] = synced_seg
            except Exception as exc:
                logger.warning("overlay: error placing TTS seg %s: %s", p, exc)
                synced_map[idx_orig] = dict(seg)

        synced_result = [synced_map.get(i, segments[i]) for i in range(len(segments))]
        if has_audio:
            master[:int(duration * 1000)].export(output_path, format="wav")
            return output_path, synced_result
        return None, synced_result
    except Exception as exc:
        logger.warning("overlay: build chunk TTS track failed: %s", exc)
        return None, segments


async def _tts_duration(path: str) -> float:
    """Độ dài file TTS (giây); 0 nếu không đọc được."""
    try:
        from core.ffmpeg import get_duration
        return await get_duration(path)
    except Exception:
        return 0.0


def _wrap_text_lines(text: str, max_chars: int = 34) -> str:
    """Wrap dialogue text with \\N so ASS subtitles stay within readable 2-line width.

    Balances split evenly across maximum 2 lines to avoid ragged or orphan words.
    Cổng chuẩn từ sever_v2/sever/services/overlay_render_service.py:1462.
    """
    clean_text = str(text or "").strip().replace("\r", "").replace("\n", " ")
    clean_text = clean_text.replace("{", "(").replace("}", ")")
    if "\\N" in clean_text:
        return clean_text
    words = [w for w in clean_text.split(" ") if w]
    if len(words) <= 1 or len(clean_text) <= max_chars:
        return clean_text

    total_len = len(clean_text)
    mid_point = total_len / 2.0
    best_split_idx = 1
    min_diff_from_mid = float("inf")

    running_len = 0
    for i in range(len(words) - 1):
        running_len += len(words[i]) + (1 if i > 0 else 0)
        diff = abs(running_len - mid_point)
        if diff < min_diff_from_mid:
            min_diff_from_mid = diff
            best_split_idx = i + 1

    line1 = " ".join(words[:best_split_idx])
    line2 = " ".join(words[best_split_idx:])
    return f"{line1}\\N{line2}"


def _build_ass(segments: list[dict], duration: float,
               target_lang: str, output_dir: str, job_id: str, ci: int,
               sub_region: dict | None = None,
               orig_w: int = 1920, orig_h: int = 1080) -> str | None:
    """Generate ASS subtitle file for this chunk's segments in output_dir.
    
    Cổng chuẩn từ sever_v2/sever/services/overlay_render_service.py:
    - PlayResX/Y theo khung hình output thật (tránh méo chữ khi 9:16).
    - Scale mọi giá trị font/margin từ baseline 1920x1080.
    - Dilated stroke (~28% font_size, outline ~3.5-11px), shadow=0.0 nét đậm chuẩn TikTok/CapCut.
    - PrimaryColour=#FFFFFF, OutlineColour=#000000, Bold=-1.
    - Alignment=2 (bottom-center).
    - Wrap cân bằng 2 dòng tại điểm giữa tránh rách chữ.
    """
    if not segments:
        return None

    font = settings.get("sub_font", "Arial")
    try:
        user_fs = int(settings.get("sub_font_size", 38))
    except (TypeError, ValueError):
        user_fs = 38
    if user_fs <= 0:
        user_fs = 38

    # Match PlayRes với khung hình video thật (tránh méo font trên video dọc 9:16 / 3:4)
    play_res_x = orig_w if orig_w > 0 else 1920
    play_res_y = orig_h if orig_h > 0 else 1080

    scale_horizontal = play_res_x / 1920.0
    scale_vertical = play_res_y / 1080.0

    # Rescale font size từ baseline 1080p
    font_size = max(16, round(user_fs * scale_vertical))

    # Dilated high-contrast stroke (CapCut/TikTok style):
    # Viền đen dày ~28% kích thước font, shadow=0 để chữ sắc nét tuyệt đối không bị nhòe
    outline_width = max(3.0, round(font_size * 0.28, 1))
    shadow = 0.0
    bold = -1
    alignment_val = 2

    margin_v = max(35, round(35 * scale_vertical))
    margin_l = max(20, round(20 * scale_horizontal))
    margin_r = max(20, round(20 * scale_horizontal))

    # Căn giữa phụ đề chính xác vào TÂM CỦA BOX BLUR (không phải giữa video)
    # Dùng \an5\pos(center_x, center_y) để căn giữa cả chiều dọc lẫn chiều ngang bên trong box blur
    pos_tag = ""
    max_chars = max(18, int(34 * scale_horizontal))

    if sub_region and float(sub_region.get("h", 0)) > 0:
        box_x = float(sub_region.get("x", 0))
        box_y = float(sub_region.get("y", 0))
        box_w = float(sub_region.get("w", play_res_x))
        box_h = float(sub_region.get("h", 0))

        # Tâm dọc và tâm ngang của box blur
        center_x = round(sub_region.get("center_x", box_x + box_w / 2.0))
        center_y = round(sub_region.get("center_y", box_y + box_h / 2.0))

        # Bảo vệ: Giữ tâm phụ đề nằm ở vùng dưới (>= 55% màn hình) để tuyệt đối không nhảy lên giữa video
        min_safe_y = int(play_res_y * 0.55)
        max_safe_y = int(play_res_y - font_size * 1.2)
        center_y = max(min_safe_y, min(max_safe_y, center_y))

        pos_tag = f"{{\\an5\\pos({center_x},{center_y})}}"

        # Giới hạn số ký tự trên 1 dòng để chữ nằm gọn bên trong box blur, không tràn viền
        usable_box_w = max(int(box_w * 0.90), int(play_res_x * 0.45))
        char_w = max(12, int(font_size * 0.52))
        max_box_chars = max(16, int(usable_box_w / char_w))
        max_chars = min(max_chars, max_box_chars)
    else:
        # Fallback vị trí đáy màn hình (86% chiều cao, KHÔNG PHẢI giữa video)
        def_x = round(play_res_x / 2.0)
        def_y = round(play_res_y - max(35, round(55 * scale_vertical)))
        pos_tag = f"{{\\an2\\pos({def_x},{def_y})}}"

    sub_style = str(settings.get("sub_style", "white_black")).lower()
    if "yellow" in sub_style:
        pri_col = "&H0000FFFF"  # Vàng
    else:
        pri_col = "&H00FFFFFF"  # Trắng

    out_col = "&H00000000"  # Viền đen
    bak_col = "&H00000000"

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{font_size},{pri_col},&H000000FF,{out_col},{bak_col},"
        f"{bold},0,0,0,100,100,0,0,1,{outline_width},{shadow},{alignment_val},"
        f"{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # Resolve overlapping segments
    cleaned = _resolve_overlaps(segments, duration)

    for seg in cleaned:
        text = seg.get("translated_text") or seg.get("text", "")
        if not text or not text.strip():
            continue
        s = max(0.0, float(seg.get("start", 0)))
        e = min(duration, float(seg.get("end", 0) or s + 2.0))
        if e <= s + 0.05:
            continue
        start_ts = _fmt_time(s)
        end_ts = _fmt_time(e)
        fit_text = _wrap_text_lines(text, max_chars=max_chars)
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{pos_tag}{fit_text}")

    ass_path = os.path.join(output_dir, f"{job_id}_ch{ci:04d}.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("overlay: ASS subtitle generated: %s (%d segments, font=%d, outline=%.1f)",
                ass_path, len(segments), font_size, outline_width)
    return ass_path


def _fit_subtitle_text(text: str, font_size: int, usable_w: int) -> str:
    """Fallback tương thích ngược: chuyển tiếp sang _wrap_text_lines."""
    char_w = max(12, int(font_size * 0.52))
    max_chars = max(16, int(usable_w / char_w))
    return _wrap_text_lines(text, max_chars)


def _fit_single_line(text: str, base_fs: int, usable_w: int) -> str:
    """Fallback tương thích ngược."""
    return _fit_subtitle_text(text, base_fs, usable_w)


def _fmt_time(seconds: float) -> str:
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _resolve_overlaps(segments: list[dict], chunk_duration: float) -> list[dict]:
    """Resolve overlapping segments so ASS subtitles don't stack on screen.

    Segments are assumed to be chunk-relative (start/end within [0, chunk_duration]).
    Strategy: sort by start time, then clip each segment's end to the next segment's
    start if they overlap. Drop segments with empty text or zero/tiny duration.
    """
    if not segments:
        return []

    valid = []
    for seg in segments:
        text = seg.get("translated_text") or seg.get("text", "")
        if not text or not text.strip():
            continue
        s = max(0.0, float(seg.get("start", 0)))
        e = min(chunk_duration, float(seg.get("end", 0) or s + 2.0))
        if e - s < 0.05:
            continue
        valid.append({**seg, "_s": s, "_e": e})

    if not valid:
        return []

    valid.sort(key=lambda x: x["_s"])

    resolved = []
    for i, seg in enumerate(valid):
        s = seg["_s"]
        e = seg["_e"]
        # Clip end to next segment's start (or chunk duration)
        if i + 1 < len(valid):
            next_start = valid[i + 1]["_s"]
            if e > next_start:
                e = next_start
        # Clip end to chunk duration
        e = min(e, chunk_duration)
        if e - s < 0.05:
            continue
        clean = dict(seg)
        clean["start"] = round(s, 3)
        clean["end"] = round(e, 3)
        clean.pop("_s", None)
        clean.pop("_e", None)
        resolved.append(clean)

    return resolved


MAX_TEMPO = 1.3
"""Trần nén tốc độ TTS — vượt mức này giọng bắt đầu méo, thà để tràn."""


async def _plan_tts_timing(segments: list[dict], chunk_start: float,
                           duration: float) -> list[dict]:
    """Cho mỗi segment có TTS: tính mốc bắt đầu, tempo nén, và mốc kết thúc thực.

    Thứ tự trả về TRÙNG với thứ tự audio_sources trong overlay_chunk (input index).
    """
    items: list[dict] = []
    for seg in segments:
        tts_path = seg.get("tts_path", "")
        if not (tts_path and os.path.exists(tts_path)):
            continue
        # ponytail: Segments are already chunk-relative, no chunk_start subtraction.
        s = max(0.0, float(seg.get("start", 0)))
        e = min(duration, float(seg.get("end", 0) or s + 2.0))
        if e <= s:
            e = min(duration, s + 2.0)
        slot = max(0.05, e - s)

        tts_len = await _tts_duration(tts_path)
        tempo = 1.0
        if tts_len > slot:
            tempo = min(MAX_TEMPO, tts_len / slot)
        real_len = (tts_len / tempo) if tts_len > 0 else slot
        items.append({
            "start": s,
            "end": min(duration, s + max(slot, real_len)),
            "tempo": tempo,
        })
        if tempo > 1.001:
            logger.info("overlay: TTS %.2fs > slot %.2fs → atempo=%.3f", tts_len, slot, tempo)
    return items


def _merge_spans(spans: list[tuple[float, float]], max_gap: float = 0.5) -> list[tuple[float, float]]:
    """Gộp các đoạn cần mute; khoảng nghỉ <= max_gap thì nối liền, > thì giữ âm gốc."""
    spans = sorted(s for s in spans if s[1] > s[0])
    if not spans:
        return []
    merged: list[tuple[float, float]] = []
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s - cur_e <= max_gap:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged


def _build_quadratic_mute_filter(spans: list[tuple[float, float]], fade_ms: float = 150.0, duck_volume: float = 0.2) -> str:
    """Build FFmpeg volume filter with quadratic (smooth) fade-in/fade-out for ducking.
    Giảm âm nền xuống mức duck_volume (20%) khi có thoại TTS để làm nổi bật giọng lồng tiếng,
    khi hết thoại thì âm thanh gốc trở lại 100% tự nhiên.
    """
    valid_spans = [(s, e) for s, e in spans if e > s + 0.001]
    if not valid_spans:
        return "[0:a]volume=1.0[bg]"

    fade_s = max(0.01, fade_ms / 1000.0)
    parts = []

    for s, e in valid_spans:
        dur = e - s
        if dur <= 2 * fade_s:
            parts.append(f"between(t,{s:.3f},{e:.3f})")
        else:
            fi_end = s + fade_s
            fo_start = e - fade_s
            parts.append(f"between(t,{s:.3f},{fi_end:.3f})*(1-((t-{s:.3f})/{fade_s:.3f})^2)")
            parts.append(f"between(t,{fi_end:.3f},{fo_start:.3f})")
            parts.append(f"between(t,{fo_start:.3f},{e:.3f})*(1-(({e:.3f}-t)/{fade_s:.3f})^2)")

    if not parts:
        return "[0:a]volume=1.0[bg]"

    mute_expr = "+".join(parts)
    drop_factor = round(1.0 - duck_volume, 3)
    return f"[0:a]volume='1-{drop_factor}*min(1,{mute_expr})':eval=frame[bg]"
