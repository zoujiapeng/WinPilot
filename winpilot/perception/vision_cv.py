"""OpenCV dimension: what OCR cannot see.

- Icon-candidate detection: contour analysis finds small square-ish UI glyphs
  (toolbar icons, title-bar buttons) and reports their coordinates.
- Template matching: locate a known image (icon screenshot in templates_img/)
  on screen with multi-scale matching.
- Color probe: dominant/average color of a point or region (button states,
  progress bars, theme changes).
- Stability / loading detection: frame differencing tells whether a region is
  still animating (spinner, video, progress) or has settled.
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from ..config import TEMPLATES_IMG_DIR
from ..utils.log import logger
from ..utils.screenshot import Region, capture
from .model import UIElement

_ICON_MIN, _ICON_MAX = 12, 64          # icon side length range (px)
_ICON_AR_TOL = 0.45                    # |1 - w/h| tolerance for square-ish shapes


def detect_icon_candidates(region: Region, max_items: int = 40) -> list[UIElement]:
    """Find small square-ish glyph regions that OCR would miss. Never raises."""
    try:
        region = region.clamp_to_screen()
        image = capture(region)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 160)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[UIElement] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (_ICON_MIN <= w <= _ICON_MAX and _ICON_MIN <= h <= _ICON_MAX):
                continue
            if abs(1 - w / h) > _ICON_AR_TOL:
                continue
            # require some ink density so we skip hollow noise boxes
            roi = edges[y : y + h, x : x + w]
            if cv2.countNonZero(roi) < w * h * 0.08:
                continue
            candidates.append(
                UIElement(
                    source="cv",
                    role="icon",
                    text="",
                    cx=region.left + x + w // 2,
                    cy=region.top + y + h // 2,
                    left=region.left + x,
                    top=region.top + y,
                    width=w,
                    height=h,
                    confidence=0.5,
                )
            )
        # de-overlap: keep larger ones first
        candidates.sort(key=lambda e: e.width * e.height, reverse=True)
        kept: list[UIElement] = []
        for cand in candidates:
            if all(cand.iou(k) < 0.3 for k in kept):
                kept.append(cand)
            if len(kept) >= max_items:
                break
        return kept
    except Exception as exc:
        logger.warning("CV 图标检测失败: %s", exc)
        return []


def match_template(
    template_name: str,
    region: Region | None = None,
    threshold: float = 0.80,
    max_matches: int = 5,
) -> list[UIElement]:
    """Multi-scale template matching against templates_img/<name>.png."""
    path = Path(template_name)
    if not path.is_absolute():
        path = TEMPLATES_IMG_DIR / template_name
    if path.suffix == "":
        path = path.with_suffix(".png")
    if not path.exists():
        logger.warning("模板不存在: %s", path)
        return []
    template = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        return []

    if region is None:
        from ..utils.screenshot import screen_size
        sw, sh = screen_size()
        region = Region(0, 0, sw, sh)
    region = region.clamp_to_screen()
    haystack = capture(region)

    results: list[UIElement] = []
    for scale in (1.0, 0.85, 1.15, 0.7, 1.3):
        th, tw = int(template.shape[0] * scale), int(template.shape[1] * scale)
        if th < 8 or tw < 8 or th >= haystack.shape[0] or tw >= haystack.shape[1]:
            continue
        scaled = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(haystack, scaled, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= threshold)
        for y, x in zip(*loc):
            results.append(
                UIElement(
                    source="cv",
                    role="image",
                    text=path.stem,
                    cx=region.left + x + tw // 2,
                    cy=region.top + y + th // 2,
                    left=region.left + int(x),
                    top=region.top + int(y),
                    width=tw,
                    height=th,
                    confidence=float(res[y, x]),
                )
            )
        if results:
            break  # best scale found; don't mix scales
    results.sort(key=lambda e: e.confidence, reverse=True)
    deduped: list[UIElement] = []
    for item in results:
        if all(item.iou(k) < 0.3 for k in deduped):
            deduped.append(item)
        if len(deduped) >= max_matches:
            break
    return deduped


def color_probe(x: int, y: int, radius: int = 4) -> dict:
    """Average + dominant color around a point. Returns hex strings."""
    region = Region(x - radius, y - radius, radius * 2 + 1, radius * 2 + 1).clamp_to_screen()
    image = capture(region)
    bgr_mean = image.reshape(-1, 3).mean(axis=0)
    avg = tuple(int(c) for c in bgr_mean[::-1])  # RGB
    pixels = image.reshape(-1, 3)
    # dominant: most frequent quantized color
    quantized = (pixels // 32 * 32)
    colors, counts = np.unique(quantized, axis=0, return_counts=True)
    dom_bgr = colors[counts.argmax()]
    dom = tuple(int(c) for c in dom_bgr[::-1])
    return {
        "average_rgb": avg,
        "average_hex": "#%02x%02x%02x" % avg,
        "dominant_rgb": dom,
        "dominant_hex": "#%02x%02x%02x" % dom,
    }


def region_mean_color(region: Region) -> tuple[int, int, int]:
    image = capture(region.clamp_to_screen())
    bgr = image.reshape(-1, 3).mean(axis=0)
    return tuple(int(c) for c in bgr[::-1])


def diff_ratio(img_a: np.ndarray, img_b: np.ndarray, pixel_thresh: int = 12) -> float:
    """Fraction of pixels that changed between two same-size BGR frames."""
    if img_a.shape != img_b.shape:
        return 1.0
    delta = cv2.absdiff(img_a, img_b)
    changed = (delta.max(axis=2) > pixel_thresh).sum()
    total = img_a.shape[0] * img_a.shape[1]
    return changed / total if total else 0.0


def wait_screen_stable(
    region: Region,
    stable_ms: int = 600,
    timeout_s: float = 15.0,
    max_change: float = 0.002,
) -> dict:
    """Block until the region stops changing (loading finished) or timeout.

    Returns {"stable": bool, "waited_s": float}.
    """
    region = region.clamp_to_screen()
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    prev = capture(region)
    stable_since: float | None = None
    while time.monotonic() < deadline:
        time.sleep(0.12)
        current = capture(region)
        if diff_ratio(prev, current) <= max_change:
            if stable_since is None:
                stable_since = time.monotonic()
            elif (time.monotonic() - stable_since) * 1000 >= stable_ms:
                return {"stable": True, "waited_s": round(time.monotonic() - started, 2)}
        else:
            stable_since = None
        prev = current
    return {"stable": False, "waited_s": round(time.monotonic() - started, 2)}


def is_animating(region: Region, sample_s: float = 0.8) -> bool:
    """Quick check: is anything moving in the region (spinner/progress)?"""
    region = region.clamp_to_screen()
    first = capture(region)
    time.sleep(sample_s / 2)
    second = capture(region)
    time.sleep(sample_s / 2)
    third = capture(region)
    return diff_ratio(first, second) > 0.001 or diff_ratio(second, third) > 0.001
