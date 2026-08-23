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
