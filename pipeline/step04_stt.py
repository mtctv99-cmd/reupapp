"""STT — ke thua logic tu sever_v2/adapters_stt.py:
- FunASR + Paraformer + VAD + Punc model
- Merge fragmented segments (character-level merge)
- Clean by audio energy (remove filler)
- Trim/align by token timestamps
- Fill segment gaps
"""
import asyncio
import json
import logging
import os
import re
import sys
import threading
import wave
from pathlib import Path
from typing import Any, Callable, Awaitable

from core.config import settings

logger = logging.getLogger(__name__)

# ── Registry ──────────────────────────────────────────────

STT_PROVIDERS: dict[str, Callable[..., Awaitable[list[dict]]]] = {}


def register_stt(name: str):
    def deco(fn):
        STT_PROVIDERS[name] = fn
        return fn
    return deco


# ── Model cache ──────────────────────────────────────────
# Load FunASR model 1 lần duy nhất, reuse cho mọi chunk.
# Trước đây AutoModel(...) load lại mỗi chunk (~30-60s/lần CPU).

_FUNASR_MODELS: dict[tuple, Any] = {}
_FUNASR_LOCK = threading.Lock()
_FUNASR_INFERENCE_LOCK = threading.Lock()


def _patch_frozen_torch_jit():
    """Khi frozen (EXE), torch.jit.script không có source để compile TorchScript
    (inspect.getsource fail -> SyntaxError). JIT chi can cho ONNX export, nen
    vô hiệu hóa để module funasr import được bình thường."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import torch.jit
        if getattr(torch.jit, "_SEVER_FROZEN_PATCHED", False):
            return

        def _noop_script(obj=None, *args, **kwargs):
            if callable(obj):
                return obj
            return _noop_script

        torch.jit.script = _noop_script
        torch.jit.script_if_tracing = _noop_script
        torch.jit._SEVER_FROZEN_PATCHED = True
        logger.info("Frozen mode: torch.jit.script disabled (no ONNX export).")
    except Exception:
        pass


_ORIG_SNAPSHOT_DOWNLOAD = None


def _patch_modelscope_offline():
    """Force modelscope local-only: models đã bundle sẵn, không cần gọi hub.
    snapshot_download hay check_local_model_is_latest vẫn request network (có thể
    hang vô hạn khi mạng flaky/không vào được aliyun)."""
    global _ORIG_SNAPSHOT_DOWNLOAD
    try:
        import modelscope.hub.snapshot_download as _ms_ss
        if not getattr(_ms_ss, "_SEVER_OFFLINE_PATCHED", False):
            _ORIG_SNAPSHOT_DOWNLOAD = _ms_ss.snapshot_download

            def _offline_ss(*args, **kwargs):
                kwargs["local_files_only"] = True
                return _ORIG_SNAPSHOT_DOWNLOAD(*args, **kwargs)

            _ms_ss.snapshot_download = _offline_ss
            _ms_ss._SEVER_OFFLINE_PATCHED = True
            logger.info("modelscope snapshot_download forced local_files_only=True")
    except Exception:
        pass
    try:
        import modelscope.hub.check_model as _ms_chk
        _ms_chk.check_local_model_is_latest = lambda *a, **k: None
        logger.info("modelscope check_local_model_is_latest disabled")
    except Exception:
        pass


def _UNPATCH_MODELSCOPE_FOR_DOWNLOAD():
    """Tạm gỡ patch local_files_only để hàm download model tải được từ internet."""
    global _ORIG_SNAPSHOT_DOWNLOAD
    try:
        import modelscope.hub.snapshot_download as _ms_ss
        if _ORIG_SNAPSHOT_DOWNLOAD is not None:
            _ms_ss.snapshot_download = _ORIG_SNAPSHOT_DOWNLOAD
            _ms_ss._SEVER_OFFLINE_PATCHED = False
    except Exception:
        pass


def _get_funasr_model(cfg: dict) -> Any:
    model_id = cfg.get("model", "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch")
    vad_id = cfg.get("vad_model", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
    punc_id = cfg.get("punc_model", "iic/punc_ct-transformer_cn-en-common-vocab471067-large")
    device = cfg.get("device") or settings.get("stt_device", "cpu")
    if device in ("auto", ""):
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    key = (model_id, vad_id, punc_id, device)
    with _FUNASR_LOCK:
        model = _FUNASR_MODELS.get(key)
        if model is None:
            # Ensure modelscope cache paths are set
            from core.models import get_models_cache_dir, check_models_installed
            cache_p = str(get_models_cache_dir())
            os.environ.setdefault("MODELSCOPE_CACHE", cache_p)
            os.environ.setdefault("HUB_CACHE", cache_p)

            _patch_frozen_torch_jit()
            # Chỉ force offline khi toàn bộ 3 model đã được tải hoàn tất
            # Nếu chưa có model mà patch offline, AutoModel sẽ lập tức văng lỗi file not found
            if check_models_installed()["ready"]:
                _patch_modelscope_offline()
            else:
                _UNPATCH_MODELSCOPE_FOR_DOWNLOAD()

            from funasr import AutoModel
            try:
                model = AutoModel(
                    model=model_id, vad_model=vad_id, punc_model=punc_id,
                    device=device, trust_remote_code=True, disable_update=True,
                )
            except Exception:
                import traceback
                logger.error("FunASR AutoModel init failed:\n%s", traceback.format_exc())
                try:
                    import funasr
                    tbs = funasr.get_import_error_tracebacks()
                    for name, tb in tbs.items():
                        if ("SyntaxError" in tb or "seaco" in name or "paraformer" in name
                                or "cif_predictor" in name):
                            logger.error("IMPORT %s:\n%s", name, tb)
                except Exception:
                    pass
                raise
            _FUNASR_MODELS[key] = model
        return model


# ── Shared segment helpers (theo V2) ─────────────────────

_FILLER_TEXTS = frozenset({"啊", "啊，", "啊。", "嗯", "嗯，", "嗯。", "呃", "呃，", "呃。", "哦", "哦，", "哦。"})

def _is_timestampless_punctuation(char: str) -> bool:
    return char in "，。！？,.!?；;：:、"

def _set_segment_end(seg: dict, new_end: float) -> None:
    seg["end"] = round(new_end, 3)
    seg["t_end"] = round(new_end, 3)
    seg["end_ms"] = round(new_end * 1000.0, 1)
    seg["duration"] = round(max(0.0, new_end - seg.get("start", 0)), 3)
    seg["duration_ms"] = round(max(0.0, (new_end - seg.get("start", 0)) * 1000.0), 1)

def _set_segment_start(seg: dict, new_start: float) -> None:
    seg["start"] = round(new_start, 3)
    seg["t_start"] = round(new_start, 3)
    seg["start_ms"] = round(new_start * 1000.0, 1)
    seg["duration"] = round(max(0.0, seg.get("end", 0) - new_start), 3)
    seg["duration_ms"] = round(max(0.0, (seg.get("end", 0) - new_start) * 1000.0), 1)


# ── STT Parameters (Tối ưu phân đoạn thoại tự nhiên cho Video tốc độ cao) ─────

_MAX_SEGMENT_CHARS = 28        # Số ký tự tiếng Trung tối đa của 1 câu thoại gọn
_TARGET_SEGMENT_SECONDS = 3.5  # Thời lượng chuẩn 1 câu thoại (2.0s - 3.5s)

# Refine — Chỉ cắt dấu ngắt mạnh (. ! ? 。 ！ ？ ； ; : ：), TRỪ dấu phẩy (chỉ cắt phẩy khi câu quá dài > 4.5s hoặc > 30 chữ)
_REFINE_THRESHOLD = 4.2
_REFINE_SPLIT_MAX = 3.5

# Split long — trần tối đa cho câu thoại TTS dài (tránh câu đọc lê thê quá 4.5s)
_SPLIT_LONG_THRESHOLD = 4.5

# Token / Silence splitting — ngắt câu khi khoảng lặng âm tiết >= 350ms
_MIN_SILENCE_TOKEN_GAP_MS = 350.0

# Merge fragmented — chỉ gom mảnh khi câu trước KHÔNG có dấu kết thúc câu (gap <= 450ms, max 3.5s / 28 ký tự)
_MERGE_MAX_GAP_MS = 450
_MERGE_TARGET_SECONDS = 3.5
_MERGE_MAX_CHARS = 28
_MERGE_SHORT_DURATION = 1.5
_MERGE_SHORT_CHARS = 6

# Audio cleanup (pydub)
_SILENCE_THRESH_OFFSET_DB = 16
_MIN_SILENCE_MS = 320
_SHORT_FILLER_MAX_DURATION = 0.5
_SHORT_TEXT_LONG_SEGMENT_CHARS = 4
_SHORT_TEXT_LONG_SEGMENT_SECONDS = 3.5
_SHORT_TEXT_ISLAND_MIN_GAP_MS = 320
_MIN_VALID_SEGMENT_DURATION = 0.45  # Lọc bỏ rác âm thanh < 0.45s
_MAX_GAP_FILL_SECONDS = 0.35


def split_by_silence_tokens(segments: list[dict], min_silence_ms: float = _MIN_SILENCE_TOKEN_GAP_MS) -> list[dict]:
    """Cắt câu dựa trên khoảng lặng >= 300ms giữa các token timestamps trong FunASR/ASR."""
    result = []
    for seg in segments:
        token_ts = seg.get("token_timestamps") or []
        text = str(seg.get("text", "") or seg.get("src_text", "")).strip()
        if not token_ts or len(token_ts) < 2 or not text:
            result.append(seg)
            continue

        char_token_map = []
        t_idx = 0
        for ch in text:
            if _is_timestampless_punctuation(ch):
                char_token_map.append(t_idx - 1 if t_idx > 0 else 0)
            else:
                char_token_map.append(t_idx)
                t_idx += 1

        splits = []
        for i in range(len(token_ts) - 1):
            gap = token_ts[i + 1][0] - token_ts[i][1]
            if gap >= min_silence_ms:
                splits.append(i + 1)

        if not splits:
            result.append(seg)
            continue

        cur_t = 0
        cur_c = 0
        for sp_t in splits:
            sp_c = len(text)
            for ci, ti in enumerate(char_token_map):
                if ti >= sp_t and ci > cur_c:
                    sp_c = ci
                    break
            sub_text = text[cur_c:sp_c].strip()
            sub_ts = token_ts[cur_t:sp_t]
            if sub_text and sub_ts:
                s_sec = round(sub_ts[0][0] / 1000.0, 3)
                e_sec = round(sub_ts[-1][1] / 1000.0, 3)
                result.append({
                    **seg,
                    "text": sub_text,
                    "src_text": sub_text,
                    "start": s_sec,
                    "end": e_sec,
                    "start_ms": sub_ts[0][0],
                    "end_ms": sub_ts[-1][1],
                    "duration": round(e_sec - s_sec, 3),
                    "duration_ms": round(sub_ts[-1][1] - sub_ts[0][0], 1),
                    "token_timestamps": list(sub_ts),
                })
            cur_t = sp_t
            cur_c = sp_c

        if cur_t < len(token_ts):
            sub_text = text[cur_c:].strip()
            sub_ts = token_ts[cur_t:]
            if sub_text and sub_ts:
                s_sec = round(sub_ts[0][0] / 1000.0, 3)
                e_sec = round(sub_ts[-1][1] / 1000.0, 3)
                result.append({
                    **seg,
                    "text": sub_text,
                    "src_text": sub_text,
                    "start": s_sec,
                    "end": e_sec,
                    "start_ms": sub_ts[0][0],
                    "end_ms": sub_ts[-1][1],
                    "duration": round(e_sec - s_sec, 3),
                    "duration_ms": round(sub_ts[-1][1] - sub_ts[0][0], 1),
                    "token_timestamps": list(sub_ts),
                })
    return result


def refine_segments(segments: list[dict]) -> list[dict]:
    """Phân đoạn thông minh cho TTS:
    1. Ưu tiên ngắt tại tất cả các dấu ngắt câu mạnh: . ! ? 。 ！ ？ ； ; : ： \n
    2. TUYỆT ĐỐI KHÔNG ngắt ở dấu phẩy (， , 、) để giữ nguyên vẹn cụm mệnh đề nghĩa.
       Chỉ cắt cưỡng bức khi một câu quá dài không có dấu chấm/chấm phẩy (split_long_segments).
    """
    refined = []
    for seg in segments:
        text = str(seg.get("text", "") or seg.get("src_text", "")).strip()
        duration = float(seg.get("duration", 0.0))

        # Tìm các điểm ngắt câu mạnh (chấm, hỏi, than, chấm phẩy, hai chấm, xuống dòng - TRỪ dấu phẩy)
        strong_pts = [m.end() for m in re.finditer(r'[.!?。！？；;:：\n]+', text)]
        strong_pts = sorted(set(p for p in strong_pts if 0 < p < len(text)))

        if strong_pts:
            subs = _split_by_points(seg, strong_pts, _REFINE_SPLIT_MAX)
            refined.extend(subs)
        else:
            refined.append(seg)
    return refined


def split_long_segments(segments: list[dict], max_duration: float = _SPLIT_LONG_THRESHOLD) -> list[dict]:
    """Cắt triệt để các câu thoại > max_duration hoặc quá nhiều ký tự thành các câu thoại ngắn gọn."""
    result = []
    for seg in segments:
        dur = float(seg.get("duration", 0.0))
        text = str(seg.get("text", "") or seg.get("src_text", "")).strip()
        if dur <= max_duration and len(text) <= _MAX_SEGMENT_CHARS:
            result.append(seg)
            continue

        token_ts = seg.get("token_timestamps")
        if token_ts:
            subs = _split_by_token_timestamps(seg, max_duration)
            if subs:
                result.extend(subs)
                continue

        punc_pts = [m.end() for m in re.finditer(r'[.!?。！？；;:：\n]+', text)]
        punc_pts = sorted(set(p for p in punc_pts if 0 < p < len(text)))
        if punc_pts:
            subs = _split_by_points(seg, punc_pts, max_duration)
            if all(s.get("duration", 0.0) <= max_duration * 1.2 for s in subs):
                result.extend(subs)
                continue

        # Nếu không có dấu câu hoặc vẫn quá dài -> Cắt đều theo thời gian & độ dài ký tự
        n = max(2, int(round(dur / max_duration)))
        sub_dur = dur / n
        chars_per_sub = max(1, len(text) // n)
        for i in range(n):
            sub = dict(seg)
            s = seg.get("start", 0) + i * sub_dur
            e = seg.get("start", 0) + (i + 1) * sub_dur if i < n - 1 else seg.get("end", 0)
            sub_text = text[i * chars_per_sub: (i + 1) * chars_per_sub if i < n - 1 else None].strip()
            if sub_text:
                sub["text"] = sub_text
                sub["src_text"] = sub_text
                _set_segment_start(sub, s)
                _set_segment_end(sub, e)
                result.append(sub)
    return result


def _split_by_token_timestamps(seg: dict, max_duration: float) -> list[dict]:
    """Split bang token timestamps (theo V2 logic)."""
    text = seg.get("text", "") or seg.get("src_text", "")
    token_ts: list = seg.get("token_timestamps", [])
    if not token_ts or not text:
        return []

    char_token_map = []
    token_idx = 0
    for char in text:
        if _is_timestampless_punctuation(char):
            char_token_map.append(token_idx - 1 if token_idx > 0 else -1)
        else:
            char_token_map.append(token_idx)
            token_idx += 1

    max_ms = max_duration * 1000.0
    subs = []
    cur_start_tok = 0
    cur_start_char = 0

    while cur_start_tok < len(token_ts):
        cur_start_ms = token_ts[cur_start_tok][0]
        split_tok = len(token_ts)
        for i in range(cur_start_tok, len(token_ts)):
            if token_ts[i][1] - cur_start_ms > max_ms and i > cur_start_tok:
                split_tok = i
                break
        split_char = len(text)
        if split_tok < len(token_ts):
            for ci, ti in enumerate(char_token_map):
                if ti >= split_tok and ci > cur_start_char:
                    split_char = ci
                    break
        sub_text = text[cur_start_char:split_char].strip()
        sub_ts = token_ts[cur_start_tok:split_tok]
        if sub_text and sub_ts:
            sub = dict(seg)
            sub["text"] = sub_text
            sub["src_text"] = sub_text
            sub["token_timestamps"] = list(sub_ts)
            _set_segment_start(sub, sub_ts[0][0] / 1000.0)
            _set_segment_end(sub, sub_ts[-1][1] / 1000.0)
            subs.append(sub)
        cur_start_tok = split_tok
        cur_start_char = split_char
    return subs


def _split_by_points(seg: dict, split_points: list[int], max_duration: float = _SPLIT_LONG_THRESHOLD) -> list[dict]:
    text = str(seg.get("text", "") or seg.get("src_text", "")).strip()
    start = float(seg.get("start", 0.0))
    end = float(seg.get("end", start))
    duration = max(0.01, end - start)
    total_len = len(text)
    if total_len == 0:
        return [seg]
    tpc = duration / total_len

    pts = sorted(set(p for p in split_points if 0 < p < total_len))
    if not pts:
        return [seg]

    subs = []
    prev_pos = 0
    prev_time = start

    for sp in pts:
        sub_text = text[prev_pos:sp].strip()
        if not sub_text:
            prev_pos = sp
            continue
        sub_dur = len(sub_text) * tpc
        sub_end = min(end, prev_time + sub_dur)
        sub = dict(seg)
        sub["text"] = sub_text
        sub["src_text"] = sub_text
        _set_segment_start(sub, prev_time)
        _set_segment_end(sub, sub_end)
        subs.append(sub)
        prev_pos = sp
        prev_time = sub_end

    if prev_pos < total_len:
        sub_text = text[prev_pos:].strip()
        if sub_text:
            sub = dict(seg)
            sub["text"] = sub_text
            sub["src_text"] = sub_text
            _set_segment_start(sub, prev_time)
            _set_segment_end(sub, end)
            subs.append(sub)

    return subs if subs else [seg]


def merge_fragmented_segments(segments: list[dict]) -> list[dict]:
    """Ghep cac cau thoai vuong vuc, noi ngat quang hoac chua het y thanh cau hoan chinh."""
    if len(segments) < 2:
        return segments

    def _single_pass(segs: list[dict]) -> tuple[list[dict], bool]:
        if len(segs) < 2:
            return segs, False
        merged = [dict(segs[0])]
        changed = False
        for seg in segs[1:]:
            prev = merged[-1]
            prev_text = str(prev.get("text") or "").strip()
            seg_text = str(seg.get("text") or "").strip()
            gap_ms = float(seg.get("start_ms", 0.0)) - float(prev.get("end_ms", 0.0))
            combined_chars = len(prev_text) + len(seg_text)
            combined_secs = float(seg.get("end", 0.0)) - float(prev.get("start", 0.0))

            prev_frag = (float(prev.get("duration") or 0.0) <= _MERGE_SHORT_DURATION
                         or len(prev_text) <= _MERGE_SHORT_CHARS)
            seg_frag = (float(seg.get("duration") or 0.0) <= _MERGE_SHORT_DURATION
                        or len(seg_text) <= _MERGE_SHORT_CHARS)

            # Khong ghep neu cau truoc da ket thuc bang dau cham / dau hoi / cham than ro rang
            ends_sentence = bool(re.search(r'[.!?。！？\n]$', prev_text))

            if (prev_text and seg_text and (prev_frag or seg_frag or not ends_sentence)
                    and 0.0 <= gap_ms <= _MERGE_MAX_GAP_MS
                    and combined_chars <= _MERGE_MAX_CHARS
                    and combined_secs <= _MERGE_TARGET_SECONDS
                    and not ends_sentence):
                joined = dict(prev)
                # Neu giua 2 cau chua co dau ngan cach thi noi lien
                joined_text = (prev_text + seg_text).strip()
                joined["text"] = joined_text
                joined["src_text"] = joined_text
                prev_ts = list(prev.get("token_timestamps") or [])
                seg_ts = list(seg.get("token_timestamps") or [])
                joined["token_timestamps"] = prev_ts + seg_ts
                _set_segment_end(joined, float(seg.get("end", prev.get("end", 0.0))))
                merged[-1] = joined
                changed = True
            else:
                merged.append(dict(seg))
        return merged, changed

    # Chay multi-pass merge (toi da 3 vong) de gom het tat ca cac manh vuon lien ke
    curr = segments
    for _ in range(3):
        curr, changed = _single_pass(curr)
        if not changed:
            break
    return curr


def filter_isolated_noise_segments(segments: list[dict]) -> list[dict]:
    """Loai bo cac am thanh rac vo nghia, tieng tho hoac tu don le < 0.35s do ASR nhan nham."""
    valid = []
    _GARBAGE_SINGLE_CHARS = frozenset({"e", "o", "a", "u", "i", "ze", "th", "磨", "得", "成", "的", "了"})
    for seg in segments:
        text = str(seg.get("text") or seg.get("src_text") or "").strip()
        dur = float(seg.get("duration") or 0.0)
        # 1. Loc segment qua ngan co ky tu rac dung co lap
        if dur < _MIN_VALID_SEGMENT_DURATION and (len(text) <= 2 or text.lower() in _GARBAGE_SINGLE_CHARS):
            continue
        # 2. Loc segment chi chua ky tu latin don le vo nghia
        if re.match(r"^[a-zA-Z]{1,2}$", text) and dur < 0.5:
            continue
        valid.append(seg)
    return valid


# ── Audio Energy Cleanup (theo V2) ───────────────────────

def clean_segments_with_audio(segments: list[dict], audio_path: str) -> list[dict]:
    """Loai filler thap nang + trim segment theo audio energy — theo V2."""
    try:
        from pydub import AudioSegment
        from pydub.silence import detect_nonsilent
    except ImportError:
        logger.debug("pydub not available, skipping audio cleanup")
        return segments

    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception:
        logger.debug("audio cleanup skipped: cannot load audio")
        return segments

    silence_thresh = audio.dBFS - _SILENCE_THRESH_OFFSET_DB if audio.dBFS != float("-inf") else -50
    cleaned = []
    for seg in segments:
        text = str(seg.get("text") or seg.get("src_text") or "").strip()
        if not text:
            continue
        seg["text"] = text
        seg["src_text"] = text

        # Loai filler thap nang
        if _is_low_energy_filler(seg, audio):
            continue

        # Split short text by audio islands
        split_segs = _split_short_text_by_audio_islands(seg, audio, silence_thresh, detect_nonsilent)
        if split_segs is not None:
            cleaned.extend(split_segs)
            continue

        # Trim segment to audio
        trimmed = _trim_segment_to_audio(seg, audio, silence_thresh, detect_nonsilent)
        cleaned.append(trimmed if trimmed else seg)
    return cleaned


def _is_low_energy_filler(seg: dict, audio) -> bool:
    text = str(seg.get("text") or seg.get("src_text") or "").strip()
    duration = float(seg.get("duration") or 0.0)
    if text not in _FILLER_TEXTS or duration > _SHORT_FILLER_MAX_DURATION:
        return False
    start_ms = max(0, int(float(seg.get("start_ms") or seg.get("start", 0.0) * 1000.0)))
    end_ms = max(start_ms, int(float(seg.get("end_ms") or seg.get("end", 0.0) * 1000.0)))
    return audio[start_ms:end_ms].dBFS == float("-inf") or audio[start_ms:end_ms].dBFS < -45


def _trim_segment_to_audio(seg: dict, audio, silence_thresh, detect_nonsilent) -> dict | None:
    start_ms = max(0, int(float(seg.get("start_ms") or seg.get("start", 0.0) * 1000.0)))
    end_ms = min(len(audio), max(start_ms, int(float(seg.get("end_ms") or seg.get("end", 0.0) * 1000.0))))
    if end_ms <= start_ms:
        return None
    nonsilent = detect_nonsilent(
        audio[start_ms:end_ms],
        min_silence_len=_MIN_SILENCE_MS,
        silence_thresh=silence_thresh,
    )
    if not nonsilent:
        return None
    first_start = start_ms + nonsilent[0][0]
    last_end = start_ms + nonsilent[-1][1]
    trimmed = dict(seg)
    _set_segment_start(trimmed, first_start / 1000.0)
    _set_segment_end(trimmed, last_end / 1000.0)

    # Re-align text using token timestamps
    token_ts = seg.get("token_timestamps")
    if token_ts:
        trimmed = _trim_text_by_token_timestamps(trimmed, token_ts, float(first_start), float(last_end))

    return trimmed if trimmed.get("end", 0.0) > trimmed.get("start", 0.0) else None


def _trim_text_by_token_timestamps(seg: dict, token_ts: list, new_start_ms: float, new_end_ms: float) -> dict:
    """Trim text and token_timestamps to only include tokens within the trimmed window."""
    text = seg.get("text", "") or seg.get("src_text", "")
    kept_indices = []
    for i, (ts_start, ts_end) in enumerate(token_ts):
        if ts_end >= new_start_ms and ts_start <= new_end_ms:
            kept_indices.append(i)

    if not kept_indices:
        seg["token_timestamps"] = []
        seg["text"] = ""
        seg["src_text"] = ""
        return seg

    kept_chars = []
    token_idx = 0
    kept_set = set(kept_indices)
    for char in text:
        if _is_timestampless_punctuation(char):
            preceding_token = token_idx - 1 if token_idx > 0 else -1
            if preceding_token in kept_set:
                kept_chars.append(char)
        else:
            if token_idx in kept_set:
                kept_chars.append(char)
            token_idx += 1

    seg["text"] = "".join(kept_chars).strip()
    seg["src_text"] = seg["text"]
    seg["token_timestamps"] = [token_ts[i] for i in kept_indices]
    return seg


def _split_short_text_by_audio_islands(seg: dict, audio, silence_thresh, detect_nonsilent) -> list[dict] | None:
    text = str(seg.get("text") or seg.get("src_text") or "").strip()
    duration = float(seg.get("duration") or 0.0)
    if len(text) > _SHORT_TEXT_LONG_SEGMENT_CHARS or duration < _SHORT_TEXT_LONG_SEGMENT_SECONDS:
        return None
    start_ms = max(0, int(float(seg.get("start_ms") or seg.get("start", 0.0) * 1000.0)))
    end_ms = min(len(audio), max(start_ms, int(float(seg.get("end_ms") or seg.get("end", 0.0) * 1000.0))))
    if end_ms <= start_ms:
        return None
    islands = detect_nonsilent(
        audio[start_ms:end_ms],
        min_silence_len=_MIN_SILENCE_MS,
        silence_thresh=silence_thresh,
    )
    if len(islands) < 2:
        return None
    if any(islands[i + 1][0] - islands[i][1] < _SHORT_TEXT_ISLAND_MIN_GAP_MS for i in range(len(islands) - 1)):
        return None
    result = []
    for i, (island_start, island_end) in enumerate(islands):
        sub = dict(seg)
        if i > 0:
            sub["text"] = ""
            sub["src_text"] = ""
        _set_segment_start(sub, (start_ms + island_start) / 1000.0)
        _set_segment_end(sub, (start_ms + island_end) / 1000.0)
        if sub.get("end", 0.0) > sub.get("start", 0.0):
            result.append(sub)
    return result or None


def _wav_duration(audio_path: str) -> float:
    try:
        with wave.open(audio_path) as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def fill_segment_gaps(segments: list[dict], audio_path: str) -> list[dict]:
    """Lam segment lien tuc bang cach chia gap tai midpoint — theo V2.

    ponytail: CHỈ kéo giãn nhẹ các khoảng nghỉ nhỏ (<= 350ms) để sub mượt,
    KHÔNG kéo giãn qua các khoảng lặng dài giữa các câu nói cách xa nhau.
    """
    valid_segs = [s for s in segments if str(s.get("text") or "").strip() and str(s.get("text") or "").strip().lower() not in ("video bilibili", "bilibili_video", "bilibili video", "bilibili")]
    if not valid_segs:
        return []
    valid_segs.sort(key=lambda s: s.get("start", 0))

    # Kéo giãn nhẹ ở đầu câu đầu tiên nếu khoảng cách < 300ms
    first = valid_segs[0]
    if 0.0 < first.get("start", 0) <= _MAX_GAP_FILL_SECONDS:
        _set_segment_start(first, 0.0)

    for i in range(len(valid_segs) - 1):
        curr_end = valid_segs[i].get("end", 0)
        next_start = valid_segs[i + 1].get("start", 0)
        gap = next_start - curr_end
        # Chỉ kéo giãn khi khoảng nghỉ nhỏ <= 350ms
        if 0.0 < gap <= _MAX_GAP_FILL_SECONDS:
            midpoint = round((curr_end + next_start) / 2.0, 3)
            _set_segment_end(valid_segs[i], midpoint)
            _set_segment_start(valid_segs[i + 1], midpoint)

    return valid_segs


# ── Provider: FunASR (theo V2) ──────────────────────────

@register_stt("funasr")
async def stt_funasr(audio_path: str, source_lang: str = "zh", **kwargs) -> list[dict]:
    """Transcribe via FunASR AutoModel (Paraformer + VAD + Punc)."""
    cfg = settings.get_stt_provider("funasr")
    logger.info("stt funasr: cfg=%s audio=%s", cfg, audio_path)

    try:
        model = _get_funasr_model(cfg)
    except Exception as err:
        logger.warning("FunASR init failed (%s), trying fallback STT...", err)
        try:
            return await stt_openai(audio_path, source_lang=source_lang, **kwargs)
        except Exception as fb_err:
            raise RuntimeError(f"STT failed: FunASR initialization error ({err}), fallback OpenAI error ({fb_err})") from err

    def _do_generate():
        with _FUNASR_INFERENCE_LOCK:
            try:
                import torch
                with torch.inference_mode():
                    return model.generate(
                        input=audio_path, batch_size=1, sentence_timestamp=True,
                        return_raw_text=True, max_single_segment_time=10000, vad_tail_silence=250,
                    )
            except Exception:
                return model.generate(
                    input=audio_path, batch_size=1, sentence_timestamp=True,
                    return_raw_text=True, max_single_segment_time=10000, vad_tail_silence=250,
                )

    result = await asyncio.to_thread(_do_generate)

    # Parse result
    if result is None:
        return []
    items = []
    if isinstance(result, list):
        items = [item for item in result if isinstance(item, dict)]
    elif isinstance(result, dict):
        items = [result]
    else:
        text = str(result).strip()
        if text:
            return [{"text": text, "src_text": text, "start": 0, "end": 0,
                     "start_ms": 0, "end_ms": 0, "duration": 0, "duration_ms": 0,
                     "t_start": 0, "t_end": 0, "token_timestamps": []}]
        return []

    segments = []
    for entry in items:
        sentence_info = entry.get("sentence_info") or []
        if isinstance(sentence_info, list) and sentence_info:
            for sentence in sentence_info:
                if not isinstance(sentence, dict):
                    continue
                text = str(sentence.get("text") or "").strip()
                if not text or text.lower() in ("video bilibili", "bilibili_video", "bilibili video", "bilibili"):
                    continue
                timestamps = []
                raw_ts = sentence.get("timestamp") or []
                for item in raw_ts:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        try:
                            s, e = float(item[0]), float(item[1])
                            if e > s:
                                timestamps.append((s, e))
                        except (TypeError, ValueError):
                            pass
                start_ms = sentence.get("start_ms") or sentence.get("start")
                end_ms = sentence.get("end_ms") or sentence.get("end")
                if timestamps:
                    start_ms = timestamps[0][0]
                    end_ms = timestamps[-1][1]
                try:
                    s_ms = float(start_ms) if start_ms is not None else 0.0
                    e_ms = float(end_ms) if end_ms is not None else s_ms + 1000.0
                except (TypeError, ValueError):
                    s_ms, e_ms = 0.0, 1000.0
                e_ms = max(e_ms, s_ms)
                seg = {
                    "text": text, "src_text": text,
                    "start": round(s_ms / 1000.0, 3), "end": round(e_ms / 1000.0, 3),
                    "start_ms": s_ms, "end_ms": e_ms,
                    "duration": round((e_ms - s_ms) / 1000.0, 3),
                    "duration_ms": round(e_ms - s_ms, 1),
                    "t_start": round(s_ms / 1000.0, 3), "t_end": round(e_ms / 1000.0, 3),
                    "token_timestamps": list(timestamps),
                }
                segments.append(seg)
        else:
            text = str(entry.get("text") or "").strip()
            if text and text.lower() not in ("video bilibili", "bilibili_video", "bilibili video", "bilibili"):
                segments.append({"text": text, "src_text": text, "start": 0, "end": 0,
                                 "start_ms": 0, "end_ms": 0, "duration": 0, "duration_ms": 0,
                                 "t_start": 0, "t_end": 0, "token_timestamps": []})

    # Apply all refinement steps
    segments = split_by_silence_tokens(segments)
    segments = refine_segments(segments)
    segments = split_long_segments(segments)
    segments = clean_segments_with_audio(segments, audio_path)
    segments.sort(key=lambda s: s.get("start", 0))
    segments = merge_fragmented_segments(segments)
    segments = filter_isolated_noise_segments(segments)
    segments = fill_segment_gaps(segments, audio_path)
    return segments


# ── Provider: OpenAI Whisper API ─────────────────────────

@register_stt("openai")
async def stt_openai(audio_path: str, source_lang: str = "zh", **kwargs) -> list[dict]:
    import httpx
    cfg = settings.get_stt_provider("openai")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "whisper-1")
    url = cfg.get("url", "https://api.openai.com/v1/audio/transcriptions")
    if not api_key:
        raise RuntimeError("OpenAI STT: API key required")
    logger.info("stt openai: model=%s", model)
    async with httpx.AsyncClient(timeout=600) as client:
        with open(audio_path, "rb") as f:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"},
                data={"model": model, "language": source_lang, "response_format": "verbose_json"},
                files={"file": (os.path.basename(audio_path), f, "audio/wav")},
            )
        resp.raise_for_status()
        data = resp.json()
    segments = []
    for seg in data.get("segments", []):
        s = {
            "text": seg.get("text", "").strip(),
            "src_text": seg.get("text", "").strip(),
            "start": seg.get("start", 0), "end": seg.get("end", 0),
            "start_ms": seg.get("start", 0) * 1000, "end_ms": seg.get("end", 0) * 1000,
            "duration": seg.get("end", 0) - seg.get("start", 0),
            "duration_ms": (seg.get("end", 0) - seg.get("start", 0)) * 1000,
            "t_start": seg.get("start", 0), "t_end": seg.get("end", 0),
            "token_timestamps": [],
        }
        segments.append(s)
    return segments


# ── Runner ───────────────────────────────────────────────

async def run_stt(audio_path: str, source_lang: str = "zh") -> list[dict]:
    provider = settings.get("stt_provider", "funasr")
    fn = STT_PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(f"Unknown STT provider: {provider}")
    return await fn(audio_path, source_lang=source_lang)


def preload_stt_model():
    """Pre-load FunASR STT model in background thread on startup so user does not wait on first job."""
    provider = settings.get("stt_provider", "funasr")
    if provider == "funasr":
        try:
            logger.info("Pre-loading FunASR STT model in background...")
            cfg = settings.get_stt_provider("funasr")
            _get_funasr_model(cfg)
            logger.info("FunASR STT model pre-loaded successfully.")
        except Exception as err:
            import traceback
            logger.warning("STT background preload notice: %s\n%s", err, traceback.format_exc())
