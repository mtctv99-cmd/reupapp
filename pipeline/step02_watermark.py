"""Step 2: detect burned-in subtitle / watermark regions via OpenCV temporal voting.
Được cổng chính xác từ sever_v2/sever/services/watermark_vision_service.py (dùng thuần OpenCV)
"""
import json
import logging
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from core.ffmpeg import _run as ffmpeg_run, get_duration, get_dimensions
from core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_SIGMA = 12


async def detect_blur_regions(video_path: str) -> list[dict[str, Any]]:
    """Detect persistent text regions = burned-in subtitles / logos.
    Returns list of {x, y, w, h, sigma, label}.
    """
    service = WatermarkVisionService()
    regions = await service.detect_regions(video_path)
    return WatermarkVisionService._normalize_regions(regions)


class WatermarkVisionService:
    """Detect persistent burned-in text regions via OpenCV temporal voting."""

    def __init__(
        self,
        *,
        ocr: Any = None,
        sample_count: int = 10,
        min_vote_ratio: float = 0.2,
        min_score: float = 0.4,
        min_width_ratio: float = 0.02,
        max_regions: int = 6,
        subtitle_width_ratio: float = 0.85,
        subtitle_min_width_ratio: float = 0.15,
        subtitle_min_y_ratio: float = 0.55,
    ) -> None:
        self._ocr = ocr
        self._sample_count = max(3, sample_count)
        self._min_vote_ratio = min_vote_ratio
        self._min_score = min_score
        self._min_width_ratio = min_width_ratio
        self._max_regions = max_regions
        self._subtitle_width_ratio = subtitle_width_ratio
        self._subtitle_min_width_ratio = subtitle_min_width_ratio
        self._subtitle_min_y_ratio = subtitle_min_y_ratio

    async def detect_regions(self, video_path: str) -> list[dict[str, Any]]:
        duration = await get_duration(video_path)
        if duration <= 0:
            return []
        width, height = await get_dimensions(video_path)
        if width <= 0 or height <= 0:
            return []

        timestamps = self._sample_timestamps(duration)
        frame_paths = await self._extract_frames(video_path, timestamps)
        try:
            per_frame = [self._detect_text_boxes(str(p)) for p in frame_paths]
            return self._vote_regions(per_frame, width, height)
        finally:
            for p in frame_paths:
                p.unlink(missing_ok=True)

    def _sample_timestamps(self, duration: float) -> list[float]:
        n = self._sample_count
        return [round(duration * (i + 1) / (n + 1), 2) for i in range(n)]

    async def _extract_frames(self, video_path: str, timestamps: list[float]) -> list[Path]:
        if not timestamps:
            return []

        ffmpeg_bin = settings.get("ffmpeg_path", "ffmpeg")
        temp_dir = Path(tempfile.gettempdir())
        prefix = f"sever-wm-{uuid.uuid4().hex}"
        paths: list[Path] = []

        for i, ts in enumerate(timestamps):
            p = temp_dir / f"{prefix}_{i+1}.jpg"
            cmd = [
                ffmpeg_bin, "-hide_banner", "-y",
                "-ss", f"{ts:.2f}",
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                str(p)
            ]
            try:
                await ffmpeg_run(cmd, timeout=30)
                if p.exists():
                    paths.append(p)
            except Exception as exc:
                logger.debug("watermark vision: frame extraction at %s failed: %s", ts, exc)

        return paths

    def _get_ocr(self) -> Any:
        """Lazy RapidOCR singleton; None khi runtime không có."""
        if self._ocr is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception as exc:
                logger.warning("watermark vision: RapidOCR unavailable: %s", exc)
                return None
            self._ocr = RapidOCR()
        return self._ocr

    def _detect_text_boxes(self, frame_path: str) -> list[dict[str, Any]]:
        """OCR primary (RapidOCR), cv2 fallback khi OCR không có — cổng từ sever_v2."""
        ocr = self._get_ocr()
        if ocr is None:
            return self._detect_text_boxes_cv2(frame_path)
        try:
            result, _elapse = ocr(frame_path)
        except Exception as exc:
            logger.debug("watermark vision: OCR failed on %s: %s", frame_path, exc)
            return []
        boxes: list[dict[str, Any]] = []
        for entry in result or []:
            try:
                points, text, score = entry[0], entry[1], float(entry[2])
            except (IndexError, TypeError, ValueError):
                continue
            try:
                xs = [float(pt[0]) for pt in points]
                ys = [float(pt[1]) for pt in points]
            except (TypeError, ValueError):
                continue
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            boxes.append({
                "x": int(x0), "y": int(y0),
                "w": int(x1 - x0), "h": int(y1 - y0),
                "score": score, "text": str(text),
            })
        return boxes

    def _detect_text_boxes_cv2(self, frame_path: str) -> list[dict[str, Any]]:
        """Best-effort text-like box detector using OpenCV glyph clustering from sever_v2."""
        try:
            import cv2
            import numpy as np
        except Exception as exc:
            logger.debug("watermark vision: OpenCV unavailable: %s", exc)
            return []

        image = cv2.imread(frame_path)
        if image is None:
            return []
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        bright = cv2.inRange(gray, 185, 255)
        dark = cv2.inRange(gray, 0, 70)
        edges = cv2.Canny(gray, 80, 180)
        mask = cv2.bitwise_or(bright, cv2.bitwise_and(edges, cv2.dilate(dark, np.ones((3, 3), np.uint8))))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8), iterations=1)
        mask = cv2.dilate(mask, np.ones((3, 11), np.uint8), iterations=1)

        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[dict[str, Any]] = []
        frame_area = float(width * height)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = float(w * h)
            if w < width * 0.025 or h < height * 0.006:
                continue
            if area > frame_area * 0.08:
                continue
            aspect = float(w) / float(max(1, h))
            if aspect < 0.8 or aspect > 30.0:
                continue

            # Text score estimate by brightness variance
            crop = gray[y:y+h, x:x+w]
            score = 0.6
            if crop.size > 0:
                std = float(np.std(crop))
                if std > 20:
                    score = min(0.95, 0.5 + (std / 100.0))

            boxes.append({"x": x, "y": y, "w": w, "h": h, "score": score})

        return boxes

    def _vote_regions(self, per_frame: list[list[dict[str, Any]]], width: int, height: int) -> list[dict[str, Any]]:
        """Temporal voting: text bands recurring across frames are burned-in subs/logos."""
        frame_count = len(per_frame)
        if frame_count == 0:
            return []

        min_width = max(2, int(width * self._min_width_ratio))

        bands: list[dict[str, Any]] = []
        for fi, detections in enumerate(per_frame):
            for box in detections:
                if box.get("score", 0) < self._min_score or box["w"] < min_width:
                    continue
                yc = box["y"] + box["h"] / 2.0
                xc = box["x"] + box["w"] / 2.0

                # Phụ đề nằm ngang ở đáy màn hình -> gom cụm theo trục Y
                # Logo/watermark cố định ở các góc -> gom cụm cả trục X và Y
                is_sub_candidate = (yc >= height * 0.65)
                tolerance_y = max(20, int(height * 0.08)) if is_sub_candidate else max(12, int(height * 0.04))
                tolerance_x = max(50, int(width * 0.15))

                band = None
                best_delta = float("inf")
                for b in bands:
                    dy = abs(b["y_center"] - yc)
                    if dy <= tolerance_y:
                        if is_sub_candidate:
                            if dy < best_delta:
                                band, best_delta = b, dy
                        else:
                            dx = abs(b.get("x_center", xc) - xc)
                            if dx <= tolerance_x and (dy + dx) < best_delta:
                                band, best_delta = b, (dy + dx)

                if band is None:
                    band = {"y_center": yc, "x_center": xc, "frames": set(), "boxes": []}
                    bands.append(band)
                band["boxes"].append(box)
                band["frames"].add(fi)
                band["y_center"] = sum(b["y"] + b["h"] / 2.0 for b in band["boxes"]) / len(band["boxes"])
                band["x_center"] = sum(b["x"] + b["w"] / 2.0 for b in band["boxes"]) / len(band["boxes"])

        min_votes = max(2, math.ceil(frame_count * self._min_vote_ratio))
        regions: list[dict[str, Any]] = []
        for band in bands:
            votes = len(band["frames"])
            if votes < min_votes:
                continue
            region = self._band_to_region(band, votes, width, height)
            if region is not None:
                regions.append(region)

        return self._filter_target_regions(regions, width, height, per_frame)[:self._max_regions]

    @staticmethod
    def _find_band(bands: list[dict[str, Any]], y_center: float, tolerance: int) -> dict[str, Any] | None:
        best, best_delta = None, float("inf")
        for band in bands:
            delta = abs(band["y_center"] - y_center)
            if delta <= tolerance and delta < best_delta:
                best, best_delta = band, delta
        return best

    def _band_to_region(self, band: dict[str, Any], votes: int, width: int, height: int) -> dict[str, Any] | None:
        boxes = band["boxes"]
        x0 = min(b["x"] for b in boxes)
        x1 = max(b["x"] + b["w"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        y1 = max(b["y"] + b["h"] for b in boxes)
        
        # Calculate median y_center to eliminate outliers
        y_centers = sorted([b["y"] + b["h"] / 2.0 for b in boxes])
        med_yc = y_centers[len(y_centers) // 2]
        avg_h = sum(b["h"] for b in boxes) / float(len(boxes))

        # Canh vừa khít phụ đề gốc: chỉ mở rộng nhẹ 2-3px để che trọn nét chữ
        pad_y = max(2, int(avg_h * 0.15))
        y0 = max(0, int(med_yc - avg_h * 0.5 - pad_y))
        y1 = min(height, int(med_yc + avg_h * 0.5 + pad_y))

        pad_x = max(8, int(width * 0.015))
        x0 = max(0, x0 - pad_x)
        x1 = min(width, x1 + pad_x)
        w, h = x1 - x0, y1 - y0
        if w < 2 or h < 2:
            return None

        # sever_v2: Giới hạn chiều cao tối đa của box blur <= 16% frame để tránh che mờ nửa màn hình
        max_allowed_h = int(height * 0.16)
        if h > max_allowed_h:
            y_mid = y0 + h / 2.0
            h = max_allowed_h
            y0 = max(0, int(round(y_mid - h / 2.0)))
            y1 = y0 + h

        yc = y0 + h / 2.0
        is_subtitle = (
            yc >= height * self._subtitle_min_y_ratio
            and w >= width * self._subtitle_min_width_ratio
            and w > h
        )
        if is_subtitle:
            # Canh chuẩn kích thước box blur vừa vặn (giảm 10% chiều ngang so với mức kéo dãn tối đa)
            target_w = min(int(width * 0.70), max(int(w * 1.08), int(width * 0.48)))
            x0 = int(round((width - target_w) / 2.0))
            x0 = max(0, min(x0, width - target_w))
            w = target_w
            label = "subtitle"
        else:
            label = "logo"

        return {"x": x0, "y": y0, "w": w, "h": h,
                "sigma": _DEFAULT_SIGMA, "label": label, "_votes": votes}

    def _filter_target_regions(self, regions: list[dict[str, Any]], width: int, height: int,
                               per_frame: list[list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        """Keep top-corner logos (left/right) + one lower-centred subtitle band — cổng từ sever_v2."""
        subtitles: list[dict[str, Any]] = []
        logos_by_side: dict[str, dict[str, Any]] = {}

        for region in regions:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("w", 0))
            h = int(region.get("h", 0))
            votes = int(region.get("_votes", 0))
            label = str(region.get("label") or "")
            y_center = y + h / 2.0

            if label == "subtitle":
                if y_center >= height * self._subtitle_min_y_ratio and w >= width * self._subtitle_min_width_ratio:
                    subtitles.append(region)
                continue

            # sever_v2 Spatial Guard: Logo/watermark cố định phải nằm sát mép biên (top <= 25% hoặc bottom >= 80%)
            # Vùng giữa màn hình (0.25 < y_center < 0.80) bỏ qua để tránh mờ nhầm chủ thể video
            if not (y_center <= height * 0.25 or y_center >= height * 0.80):
                continue

            is_top = y <= height * 0.22 and h <= height * 0.18 and w <= width * 0.35
            side = "left" if x <= width * 0.35 else "right" if x + w >= width * 0.65 else ""
            if not is_top or not side:
                continue

            current = logos_by_side.get(side)
            current_score = (
                int(current.get("_votes", 0)),
                int(current.get("w", 0)) * int(current.get("h", 0)),
            ) if current is not None else (-1, -1)
            new_score = (votes, w * h)
            if new_score > current_score:
                logos_by_side[side] = region

        selected: list[dict[str, Any]] = []
        for side in ("left", "right"):
            region = logos_by_side.get(side)
            if region is not None:
                selected.append(region)

        # Fallback an toàn: nếu chưa có subtitle band nào mà user bật xóa sub
        if not subtitles:
            # Thu thập mọi box ở nửa dưới màn hình (y >= height * 0.65)
            bottom_boxes = []
            if per_frame:
                for fi, detections in enumerate(per_frame):
                    for box in detections:
                        if (box.get("y", 0) + box.get("h", 0) / 2.0) >= height * 0.65 and box.get("w", 0) >= width * 0.10:
                            bottom_boxes.append(box)
            if bottom_boxes:
                # Lấy box dài nhất / lớn nhất
                longest_box = max(bottom_boxes, key=lambda b: b.get("w", 0))
                bw = min(int(width * 0.90), max(int(longest_box["w"] * 1.15), int(width * 0.50)))
                bh = max(int(longest_box["h"] * 1.25), int(height * 0.065))
                bx = max(0, int((width - bw) / 2.0))
                by = max(int(height * 0.65), min(int(height - bh - 10), int(longest_box["y"] - int(bh * 0.1))))
                subtitles.append({
                    "x": bx, "y": by, "w": bw, "h": bh,
                    "sigma": _DEFAULT_SIGMA, "label": "subtitle", "_votes": 1
                })
            else:
                # Default subtitle blur band ở đáy màn hình
                def_w = int(width * 0.65)
                def_h = max(36, int(height * 0.075))
                def_x = max(0, int((width - def_w) / 2.0))
                def_y = max(0, int(height * 0.86 - def_h / 2.0))
                subtitles.append({
                    "x": def_x, "y": def_y, "w": def_w, "h": def_h,
                    "sigma": _DEFAULT_SIGMA, "label": "subtitle", "_votes": 1
                })

        if subtitles:
            # Ưu tiên dải subtitle có nhiều vote nhất (xuất hiện ở nhiều frame nhất),
            # tiếp theo là vị trí y_center nằm sát đáy màn hình hơn (subtitles chuẩn) và chiều rộng w
            best_sub = max(subtitles, key=lambda item: (
                int(item.get("_votes", 0)),
                int(item.get("y", 0) + item.get("h", 0) / 2.0),
                int(item.get("w", 0))
            ))
            selected.append(best_sub)

        selected.sort(key=lambda item: (item.pop("_votes", 0), item["w"]), reverse=True)
        return selected

    @staticmethod
    def _normalize_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Lọc region rác + ép kiểu chuẩn {x,y,w,h,sigma,label} — cổng từ sever_v2 worker."""
        normalized: list[dict[str, Any]] = []
        for region in regions:
            if not isinstance(region, dict):
                continue
            try:
                x = max(0, int(region.get("x", 0)))
                y = max(0, int(region.get("y", 0)))
                w = int(region.get("w", region.get("width", 0)))
                h = int(region.get("h", region.get("height", 0)))
                sigma = max(0, int(region.get("sigma", 10)))
            except (TypeError, ValueError):
                continue
            if w < 2 or h < 2:
                continue
            clean = {"x": x, "y": y, "w": w, "h": h, "sigma": sigma}
            if region.get("label"):
                clean["label"] = str(region["label"])
            normalized.append(clean)
        return normalized
