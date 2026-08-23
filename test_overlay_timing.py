"""Self-check cho timing TTS trong step07_overlay. Chay: python test_overlay_timing.py"""
import asyncio
import os
import tempfile

import pipeline.step07_overlay as ov


def test_merge_spans():
    assert ov._merge_spans([]) == []
    # nghi 0.3s <= max_gap -> gop
    assert ov._merge_spans([(0, 1), (1.3, 2)]) == [(0, 2)]
    # nghi 1.0s > max_gap -> giu rieng
    assert ov._merge_spans([(0, 1), (2.0, 3)]) == [(0, 1), (2.0, 3)]
    # khong theo thu tu + span rong bi loai
    assert ov._merge_spans([(5, 6), (0, 1), (2, 2)]) == [(0, 1), (5, 6)]


def test_plan_tts_timing():
    """Test timing calculations with mocked TTS file durations."""
    paths = {}
    for dur in (1.0, 3.0, 2.2):
        fd, p = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        paths[p] = dur

    try:
        ov._tts_duration = lambda p: _const(paths[p])

        f_short, f_long, f_huge = list(paths.keys())
        segs = [
            {"start": 0.0, "end": 2.0, "tts_path": f_short},   # 1.0s / slot 2.0s -> tempo 1.0
            {"start": 10.0, "end": 12.0, "tts_path": f_long},  # 3.0s / slot 2.0s -> tempo 1.5 -> tran 1.3
            {"start": 20.0, "end": 22.0, "tts_path": f_huge},  # 2.2s / slot 2.0s -> tempo 1.1
            {"start": 30.0, "end": 32.0},                      # khong co TTS -> bo qua
        ]
        items = asyncio.run(ov._plan_tts_timing(segs, chunk_start=0.0, duration=60.0))

        assert len(items) == 3, "chi segment co tts_path moi vao danh sach"
        assert items[0]["tempo"] == 1.0
        assert items[0]["start"] == 0.0
        assert items[1]["tempo"] == ov.MAX_TEMPO, "vuot tran phai bi kep lai 1.3x"
        # 3.0/1.3 = 2.31s > slot 2.0 -> end noi rong theo do dai thuc
        assert items[1]["end"] > items[1]["start"] + 2.0
        assert abs(items[2]["tempo"] - 1.1) < 1e-6
        assert items[2]["start"] == 20.0
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


def _const(v):
    async def _f():
        return v
    return _f()


if __name__ == "__main__":
    test_merge_spans()

    paths = {}
    for dur in (1.0, 3.0, 2.2):
        fd, p = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        paths[p] = dur
    try:
        test_plan_tts_timing(paths)
    finally:
        for p in paths:
            os.remove(p)
    print("OK")
