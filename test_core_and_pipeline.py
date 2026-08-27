import pytest
from core.config import settings, app_dir, get_bin_dir
from pipeline.step05_translate import _parse_translate_response, _unpack_sse_response, _parse_indexed_items
from pipeline.step07_overlay import _resolve_overlaps, _build_quadratic_mute_filter, _merge_spans

def test_config_basics():
    settings.set("custom_key", "test_val")
    assert settings.get("custom_key") == "test_val"
    assert app_dir().exists()

def test_step05_parse_indexed_json():
    original = ["你好", "世界"]
    json_response = '[{"index": 0, "translated": "Xin chào"}, {"index": 1, "translated": "Thế giới"}]'
    parsed = _parse_translate_response(json_response, original)
    assert parsed == ["Xin chào", "Thế giới"]

def test_step05_parse_markdown_wrapped_json():
    original = ["你好", "再见"]
    raw_response = 'Here is the translation:\n```json\n[{"index": 0, "translated": "Chào bạn"}, {"index": 1, "translated": "Tạm biệt"}]\n```'
    parsed = _parse_translate_response(raw_response, original)
    assert parsed == ["Chào bạn", "Tạm biệt"]

def test_step05_parse_mismatched_indices():
    original = ["Câu 1", "Câu 2"]
    # Missing index 1
    bad_json = '[{"index": 0, "translated": "Xin chào"}]'
    assert _parse_translate_response(bad_json, original) is None

def test_step05_sse_parsing():
    raw_sse = 'data: {"choices":[{"delta":{"content":"Hello"}}]}\ndata: {"choices":[{"delta":{"content":" World"}}]}\ndata: [DONE]'
    extracted = _unpack_sse_response(raw_sse)
    assert extracted == "Hello World"

def test_step07_resolve_overlaps():
    # Overlapping intervals should be clipped
    segs = [
        {"start": 0.0, "end": 2.5, "text": "Seg 1"},
        {"start": 2.0, "end": 4.0, "text": "Seg 2"},
    ]
    resolved = _resolve_overlaps(segs, chunk_duration=10.0)
    assert len(resolved) == 2
    assert resolved[0]["start"] == 0.0
    assert resolved[0]["end"] == 2.0  # clipped to next seg start
    assert resolved[1]["start"] == 2.0
    assert resolved[1]["end"] == 4.0

def test_step07_merge_spans():
    spans = [(0.0, 1.0), (1.2, 2.0), (5.0, 6.0)]
    # 1.0 to 1.2 is gap of 0.2 <= 0.5 -> should merge
    merged = _merge_spans(spans, max_gap=0.5)
    assert merged == [(0.0, 2.0), (5.0, 6.0)]


def test_step02_filter_target_regions_fallback():
    from pipeline.step02_watermark import WatermarkVisionService
    service = WatermarkVisionService()
    # Empty regions with empty per_frame -> should generate default subtitle band without NameError
    res = service._filter_target_regions([], 1920, 1080, per_frame=[])
    assert len(res) >= 1
    assert res[0]["label"] == "subtitle"


def test_step06_tts_clean_and_normalize():
    from pipeline.step06_tts import _clean_for_tts, _normalize_voice
    raw_text = '“Xin chào” "thế giới"！'
    cleaned = _clean_for_tts(raw_text)
    assert '"' not in cleaned and '“' not in cleaned and '”' not in cleaned
    assert _normalize_voice("tiktok_female") == "BV074_streaming"
    assert _normalize_voice("tiktok_male") == "BV001_streaming"


def test_step07_smart_word_wrap():
    from pipeline.step07_overlay import _fit_subtitle_text
    # Short sentence -> stays 1 line
    short_text = "Xin chào các bạn"
    res_short = _fit_subtitle_text(short_text, font_size=38, usable_w=1200)
    assert "\\N" not in res_short
    assert res_short == short_text

    # Long sentence in narrow container -> wraps into 2 balanced lines with \N
    long_text = "Hôm nay chúng ta sẽ cùng nhau tìm hiểu về toàn bộ bí mật của tông môn này"
    res_long = _fit_subtitle_text(long_text, font_size=38, usable_w=400)
    assert "\\N" in res_long
    lines = res_long.split("\\N")
    assert len(lines) == 2
    # Verify no lost words
    assert " ".join(lines) == long_text


