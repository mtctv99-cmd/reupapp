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

    # Build ASS subtitle file inside output_dir passing original dimensions
    ass_path = _build_ass(segments, duration, target_lang, output_dir, job_id, ci, sub_region, orig_w, orig_h)

    # Build single master TTS audio track via pydub
    # Tránh lỗi FFmpeg exit 4294967274 khi số lượng input streams/filters vượt giới hạn (>100 files)
    tts_master_path = os.path.join(output_dir, f"{job_id}_ch{ci:04d}_tts_mix.wav")
    has_tts = _build_chunk_tts_track(segments, duration, tts_master_path) is not None

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
        # Use forward slashes + escape colons for FFmpeg filter syntax (single quote & backslash)
        clean_ass = ass_path.replace("\\", "/")
        clean_ass = clean_ass.replace("'", "'\\''")
        if len(clean_ass) > 2 and clean_ass[1] == ":":
            clean_ass = clean_ass[0] + "\\:" + clean_ass[2:]
        next_v = "[vsub]"
        v_chains.append(f"{curr_v}subtitles=filename='{clean_ass}'{next_v}")
        curr_v = next_v

    # D. SCALE/PAD (Chỉ áp dụng khi user chọn tỉ lệ khác; mặc định 'original' giữ nguyên 100% màu sắc & nét gốc)
    aspect_ratio = str(settings.get("aspect_ratio", "original")).lower()
    if "original" in aspect_ratio or not aspect_ratio:
        scale_str = ""
    elif "3:4" in aspect_ratio:
        scale_str = "scale=720:960:force_original_aspect_ratio=decrease,pad=720:960:(720-iw)/2:(960-ih)/2"
    elif "9:16" in aspect_ratio:
        scale_str = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(720-iw)/2:(1280-ih)/2"
    else:
        scale_str = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(1280-iw)/2:(720-ih)/2"

    if scale_str:
        zoom_out = "[vzoom]"
        v_chains.append(f"{curr_v}{scale_str}{zoom_out}")
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
                # sidechaincompress tự động hạ âm nền gốc xuống khi có tiếng TTS
                fc_parts.append(f"[{tts_input_idx}:a]asplit=2[tts_sc][tts_mix]")
                fc_parts.append(f"[0:a]volume={orig_vol_val:.2f}[bg_vol]")
                fc_parts.append("[bg_vol][tts_sc]sidechaincompress=threshold=0.01:ratio=5:attack=50:release=250[bg]")
                fc_parts.append("[bg][tts_mix]amix=inputs=2:duration=first:normalize=0[aout]")
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
    cmd.extend(["-pix_fmt", "yuv420p"])
    if audio_map or src_has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    cmd.append(output)
    logger.info("overlay_chunk cmd: %s", " ".join(cmd))
    await ffmpeg_run(cmd, timeout=3600)
    if not os.path.exists(output):
        raise RuntimeError(f"FFmpeg failed to create overlay output file: {output}")
    return output


def _build_chunk_tts_track(segments: list[dict], duration: float, output_path: str) -> str | None:
    """Mix all TTS segments of a chunk into a single stereo WAV file.
    Prevents FFmpeg from hitting input stream limits (error 4294967274) when mixing >100 segments.
    """
    if not segments:
        return None
    try:
        from pydub import AudioSegment
        from pydub.effects import speedup
        AudioSegment.converter = settings.get("ffmpeg_path", "ffmpeg")

        track_ms = int(max(1.0, duration + 0.5) * 1000)
        master = AudioSegment.silent(duration=track_ms, frame_rate=44100).set_channels(2)
        has_audio = False

        for seg in segments:
            p = seg.get("tts_path")
            if not (p and os.path.exists(p)):
                continue
            try:
                audio = AudioSegment.from_file(p).set_frame_rate(44100).set_channels(2)
                # Boost volume to 1.5x (~ +3.5 dB)
                audio = audio + 3.5
                s = max(0.0, float(seg.get("start", 0)))
                e = min(duration, float(seg.get("end", 0) or s + 2.0))
                if e <= s:
                    e = min(duration, s + 2.0)
                slot = max(0.05, e - s)
                cur_dur = len(audio) / 1000.0
                if cur_dur > slot:
                    tempo = min(MAX_TEMPO, cur_dur / slot)
                    if tempo > 1.02 and len(audio) > 150:
                        try:
                            audio = speedup(audio, playback_speed=tempo)
                        except Exception:
                            pass
                pos_ms = max(0, int(s * 1000))
                master = master.overlay(audio, position=pos_ms)
                has_audio = True
            except Exception:
                pass

        if has_audio:
            master[:int(duration * 1000)].export(output_path, format="wav")
            return output_path
    except Exception as exc:
        logger.warning("overlay: build chunk TTS track failed: %s", exc)
    return None


async def _tts_duration(path: str) -> float:
    """Độ dài file TTS (giây); 0 nếu không đọc được."""
    try:
        from core.ffmpeg import get_duration
        return await get_duration(path)
    except Exception:
        return 0.0


def _build_ass(segments: list[dict], duration: float,
               target_lang: str, output_dir: str, job_id: str, ci: int,
               sub_region: dict | None = None,
               orig_w: int = 1920, orig_h: int = 1080) -> str | None:
    """Generate ASS subtitle file for this chunk's segments in output_dir."""
    if not segments:
        return None

    font = settings.get("sub_font", "Arial")
    try:
        user_fs = int(settings.get("sub_font_size", 38))
    except (TypeError, ValueError):
        user_fs = 38
    if user_fs <= 0:
        user_fs = 38

    sub_style = str(settings.get("sub_style", "white_black")).lower()
    is_box_style = ("box" in sub_style)

    box_h = 0
    if sub_region:
        box_h = int(sub_region.get("h", 0) or 0)

    # Căn chỉnh font size và độ dày nền/viền phụ đề tự động khớp chiều cao với box blur
    if box_h > 15:
        if is_box_style:
            # Dạng hộp (Box style): Font chữ chiếm ~65% chiều cao, phần còn lại làm padding hộp đen che trọn sub gốc
            font_size = max(20, min(int(box_h * 0.65), 70))
            outline_val = max(2, int((box_h - font_size) / 2.0))
            shadow_val = 0
            border_style = 3
        else:
            # Dạng viền chữ (Outline style): Font chữ chiếm ~75% chiều cao box blur
            font_size = max(22, min(int(box_h * 0.75), 70))
            outline_val = max(2, min(6, int((box_h - font_size) / 4.0)))
            shadow_val = 1
            border_style = 1
    else:
        font_size = user_fs
        outline_val = 2
        shadow_val = 0 if is_box_style else 1
        border_style = 3 if is_box_style else 1

    pos_tag = ""
    if sub_region:
        box_x = float(sub_region.get("x", 0))
        box_y = float(sub_region.get("y", 0))
        box_w_val = float(sub_region.get("w", 0))
        box_h_val = float(sub_region.get("h", 0))
        center_x = int(box_x + (box_w_val / 2.0)) if box_w_val > 50 else int(orig_w / 2.0)
        center_y = int(box_y + (box_h_val / 2.0))
        pos_tag = f"{{\\an5\\pos({center_x},{center_y})}}"

    margin_v = int(orig_h * 0.05)
    alignment_val = 2

    # Giới hạn bề rộng chữ = chiều rộng box blur của sub gốc (nếu có)
    usable_w = int(orig_w * 0.9)
    if sub_region:
        box_w = int(sub_region.get("w", 0) or 0) + 24
        if box_w > 40:
            usable_w = min(usable_w, box_w)

    if "yellow_box" in sub_style:
        # Chữ vàng nền hộp đen mờ
        pri_col = "&H0000FFFF"
        out_col = "&H00000000"
        bak_col = "&H60000000"
    elif "white_box" in sub_style:
        # Chữ trắng nền hộp đen mờ
        pri_col = "&H00FFFFFF"
        out_col = "&H00000000"
        bak_col = "&H60000000"
    elif "yellow" in sub_style:
        # Chữ vàng viền đen
        pri_col = "&H0000FFFF"
        out_col = "&H00000000"
        bak_col = "&H00000000"
    else:
        # Mặc định: Chữ trắng viền đen sắc nét
        pri_col = "&H00FFFFFF"
        out_col = "&H00000000"
        bak_col = "&H00000000"

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {orig_w}",
        f"PlayResY: {orig_h}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        f" OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        f" ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        f" Alignment, MarginL, MarginR, MarginV, Encoding",
    ]

    if is_box_style:
        # Tách làm 2 layers độc lập:
        # Layer 0 (SubBox): Nền hộp bo tròn góc êm (\blur5 trên viền hộp, chữ trong suốt 100%)
        # Layer 1 (SubText): Chữ phụ đề sắc nét, rõ ràng 100%, không bị dính mờ
        lines.append(
            f"Style: SubBox,{font},{font_size},&HFF000000,&H000000FF,"
            f"{out_col},{bak_col},1,0,0,0,100,100,0,0,3,{outline_val},0,"
            f"{alignment_val},10,10,{margin_v},1"
        )
        lines.append(
            f"Style: SubText,{font},{font_size},{pri_col},&H000000FF,"
            f"&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,"
            f"{alignment_val},10,10,{margin_v},1"
        )
    else:
        # Kiểu chữ viền đen truyền thống sắc nét
        lines.append(
            f"Style: Default,{font},{font_size},{pri_col},&H000000FF,"
            f"{out_col},{bak_col},1,0,0,0,100,100,0,0,{border_style},{outline_val},{shadow_val},"
            f"{alignment_val},10,10,{margin_v},1"
        )

    lines.extend([
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])

    # ponytail: Resolve overlapping segments so ASS doesn't stack dialogue lines.
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
        fit_text = _fit_single_line(text, font_size, usable_w)

        if is_box_style:
            # Layer 0: Vẽ nền hộp bo tròn mềm
            box_tag = f"{pos_tag[:-1]}\\blur5}}" if pos_tag else "{\\blur5}"
            lines.append(f"Dialogue: 0,{start_ts},{end_ts},SubBox,,0,0,0,,{box_tag}{fit_text}")
            # Layer 1: Vẽ chữ phụ đề sắc nét đè lên trên nền
            text_tag = f"{pos_tag}" if pos_tag else ""
            lines.append(f"Dialogue: 1,{start_ts},{end_ts},SubText,,0,0,0,,{text_tag}{fit_text}")
        else:
            line_content = f"{pos_tag}{fit_text}" if pos_tag else fit_text
            lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{line_content}")

    ass_path = os.path.join(output_dir, f"{job_id}_ch{ci:04d}.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("overlay: ASS subtitle generated: %s (%d segments)", ass_path, len(segments))
    return ass_path


def _fit_single_line(text: str, base_fs: int, usable_w: int) -> str:
    """Cố định cỡ chữ phụ đề (size 32), không tự động tăng giảm font lộn xộn."""
    text = str(text).strip()
    return text.replace("\n", " ")


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
