import unittest
from core.config import settings
from pipeline.step07_overlay import _build_quadratic_mute_filter, _merge_spans

class TestPipeline(unittest.TestCase):
    def test_settings_load(self):
        # ponytail: verify config property exists and returns dict
        self.assertIsNotNone(settings.data)
        self.assertIn("download_dir", settings.data)

    def test_mute_filter_no_nan(self):
        # ponytail: verify mute filter output format for invalid/edge-case spans
        spans = [(0.0, 0.0), (1.0, 1.05), (2.0, 5.0)]
        filt = _build_quadratic_mute_filter(spans)
        self.assertIn("volume=", filt)
        self.assertNotIn("NaN", filt)

    def test_tts_save_audio(self):
        # ponytail: verify base64/bytes audio save helper used by TTS server path
        import base64
        import os
        from pipeline.step06_tts import _save_audio_data

        raw = b"ID3fake-audio-bytes"
        path = _save_audio_data(base64.b64encode(raw).decode())
        try:
            with open(path, "rb") as f:
                self.assertEqual(f.read(), raw)
        finally:
            os.remove(path)
        self.assertRaises(ValueError, _save_audio_data, "not-base64!!")


if __name__ == "__main__":
    unittest.main()
