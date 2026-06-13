"""OCR dimension: RapidOCR (PP-OCR v3) with DirectML GPU acceleration.

RapidOCR 1.2.x builds CPU-only sessions; we rebuild its internal
``ort.InferenceSession`` objects with DmlExecutionProvider (AMD 780M via
DirectML) and gracefully fall back to CPU if that fails. Recognition runs on
a window-region screenshot and bounding boxes are mapped back to absolute
screen coordinates.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import onnxruntime as ort

from ..config import CONFIG
from ..utils.log import logger
from ..utils.screenshot import Region, capture
from .model import UIElement

_engine = None
_engine_lock = threading.Lock()
_active_provider = "uninitialized"


def _rebuild_with_dml(engine) -> bool:
    """Swap CPU InferenceSessions inside RapidOCR for DML ones. True if all swapped."""
    if "DmlExecutionProvider" not in ort.get_available_providers():
        return False
    options = ort.SessionOptions()
    # DirectML EP requirements: no mem pattern, sequential execution.
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    providers = ["DmlExecutionProvider", "CPUExecutionProvider"]

    swapped = 0
    for part_name in ("text_detector", "text_recognizer", "text_cls"):
        part = getattr(engine, part_name, None)
        wrapper = getattr(part, "session", None)
        inner = getattr(wrapper, "session", None)
        if not isinstance(inner, ort.InferenceSession):
            continue
        model_path = getattr(inner, "_model_path", None)
        if not model_path:
            return False
        try:
            wrapper.session = ort.InferenceSession(
                model_path, sess_options=options, providers=providers
            )
            swapped += 1
        except Exception as exc:
            logger.warning("OCR: %s 切换 DML 失败: %s", part_name, exc)
            return False
    return swapped > 0


def get_engine():
    """Lazy-init the shared RapidOCR engine (thread-safe)."""
    global _engine, _active_provider
    with _engine_lock:
        if _engine is not None:
            return _engine
        from rapidocr_onnxruntime import RapidOCR

        started = time.monotonic()
        engine = RapidOCR()
        if CONFIG.get("ocr", "use_gpu", default=True) and _rebuild_with_dml(engine):
            _active_provider = "DmlExecutionProvider"
        else:
            _active_provider = "CPUExecutionProvider"
        # Warm up once so first real call isn't slow.
        try:
            engine(np.full((64, 256, 3), 255, dtype=np.uint8))
        except Exception as exc:
            logger.warning("OCR 预热失败: %s", exc)
        logger.info(
            "OCR 引擎就绪 provider=%s 耗时=%.1fs",
            _active_provider, time.monotonic() - started,
        )
        _engine = engine
        return _engine


def active_provider() -> str:
    return _active_provider


def recognize(image: np.ndarray, origin: tuple[int, int] = (0, 0),
              min_conf: float | None = None) -> list[UIElement]:
    """OCR an BGR image; map results to screen coords using `origin` offset.

    ``min_conf=None`` uses the configured threshold; escalation/retry paths
    pass a lower value so faint or small text isn't filtered out when the
    normal pass came back empty.
    """
    engine = get_engine()
    if min_conf is None:
        min_conf = float(CONFIG.get("ocr", "min_confidence", default=0.55))
    ox, oy = origin
    try:
        result, _elapsed = engine(image)
    except Exception as exc:
        logger.warning("OCR 识别异常: %s", exc)
        return []
    if not result:
        return []
    elements: list[UIElement] = []
    for box, text, score in result:
        score = float(score)
        if score < min_conf or not str(text).strip():
            continue
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        left, top = min(xs) + ox, min(ys) + oy
        width, height = max(xs) - min(xs), max(ys) - min(ys)
        elements.append(
            UIElement(
                source="ocr",
                role="text",
                text=str(text).strip(),
                cx=left + width // 2,
                cy=top + height // 2,
                left=left,
                top=top,
                width=width,
                height=height,
                confidence=score,
            )
        )
    return elements


def collect(region: Region, min_conf: float | None = None) -> list[UIElement]:
    """OCR a screen region (e.g. the target window). Never raises."""
    try:
        region = region.clamp_to_screen()
        image = capture(region)
        return recognize(image, origin=(region.left, region.top), min_conf=min_conf)
    except Exception as exc:
        logger.warning("OCR collect 失败: %s", exc)
        return []


def find_text(region: Region, needle: str,
              min_conf: float | None = None) -> list[UIElement]:
    """All OCR hits containing the substring (case-insensitive)."""
    needle_lower = needle.lower()
    return [e for e in collect(region, min_conf=min_conf)
            if needle_lower in e.text.lower()]
