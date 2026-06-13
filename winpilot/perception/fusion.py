"""Perception fusion: run enabled dimensions in priority order, merge,
dedupe, and produce both a structured snapshot and a compact text rendering
for the LLM.

Element IDs are stable within one observation; the agent refers to elements
as ``click(element_id=12)`` and the executor resolves them through the
snapshot registry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import CONFIG
from ..utils.log import BUS, logger
from ..utils.screenshot import Region, screen_size, window_region
from . import ocr, uia, vision_cv, vlm, win32
from .model import UIElement

_DEDUP_IOU = 0.5


@dataclass
class Snapshot:
    """One observation of a window: fused elements + metadata."""

    hwnd: int
    title: str
    region: Region
    elements: list[UIElement] = field(default_factory=list)
    dim_stats: dict[str, dict] = field(default_factory=dict)  # per-dimension count+ms
    created_at: float = field(default_factory=time.time)

    def by_id(self, elem_id: int) -> UIElement | None:
        for element in self.elements:
            if element.elem_id == elem_id:
                return element
        return None

    def to_text(self, max_elements: int = 160) -> str:
        lines = [
            f"窗口: \"{self.title}\" hwnd={self.hwnd} "
            f"rect=({self.region.left},{self.region.top},{self.region.width}x{self.region.height})"
        ]
        stats = " ".join(
            f"{name}:{stat['count']}个/{stat['ms']}ms" for name, stat in self.dim_stats.items()
        )
        if stats:
            lines.append(f"感知维度: {stats}")
        # Canvas/visual blind-spot: a large window yielding almost no structural
        # elements is almost certainly canvas/game/video — tell the model so it
        # uses VLM vision instead of blindly guessing coordinates.
        readable = sum(1 for e in self.elements if (e.text or "").strip())
        big = self.region.width * self.region.height > 200_000
        if readable < 3 and big:
            lines.append(
                "⚠️ 此界面结构化感知几乎为空（可能是 canvas/游戏/视频/自绘内容）——"
                "UIA/OCR/CV 读不到，请用 vlm_describe 看实际画面，不要靠坐标/颜色盲猜")
        shown = self.elements[:max_elements]
        lines.extend(element.to_line() for element in shown)
        if len(self.elements) > max_elements:
            lines.append(f"...还有 {len(self.elements) - max_elements} 个元素已省略")
        return "\n".join(lines)


def _merge(primary: list[UIElement], secondary: list[UIElement]) -> list[UIElement]:
    """Append secondary elements that don't duplicate a primary one.

    A secondary element is a duplicate if it overlaps (IoU) a primary element
    AND adds no new text. OCR text that a UIA element lacks is grafted onto
    the UIA element instead of being dropped.
    """
    merged = list(primary)
    for cand in secondary:
        duplicate = False
        for kept in merged:
            if cand.iou(kept) >= _DEDUP_IOU:
                if cand.text and not kept.text:
                    kept.text = cand.text  # graft OCR text onto unnamed element
                if cand.text and kept.text and cand.text.strip() in kept.text:
                    duplicate = True
                    break
                if not cand.text:
                    duplicate = True
                    break
                if cand.text == kept.text:
                    duplicate = True
                    break
        if not duplicate:
            merged.append(cand)
    return merged


def _has_readable(elements: list[UIElement]) -> bool:
    return any((e.text or "").strip() for e in elements)


# Lowered OCR confidence for rescue passes — the normal threshold already
# returned nothing, so trade precision for recall.
_ESCALATE_OCR_CONF = 0.4


def _escalate_empty(snapshot: Snapshot, region: Region,
                    fused: list[UIElement]) -> list[UIElement]:
    """Rescue path when perception came back blind (no readable element).

    The agent cannot act on an empty screen — before returning it, retry with
    progressively stronger fallbacks: window OCR at lowered confidence →
    full-screen OCR → VLM (when configured). Controlled by
    ``fusion.escalate_on_empty`` (turn off for dimension-isolation testing).
    """
    steps: list[tuple[str, object]] = [
        ("esc_ocr_window", lambda: ocr.collect(region, min_conf=_ESCALATE_OCR_CONF)),
        ("esc_ocr_screen", lambda: ocr.collect(
            Region(0, 0, *screen_size()), min_conf=_ESCALATE_OCR_CONF)),
    ]
    if vlm.is_configured():
        steps.append(("esc_vlm", lambda: vlm.collect(region)))

    for name, collect_fn in steps:
        started = time.monotonic()
        try:
            collected = collect_fn()
        except Exception as exc:
            logger.warning("空快照升级 %s 异常: %s", name, exc)
            collected = []
        elapsed_ms = int((time.monotonic() - started) * 1000)
        snapshot.dim_stats[name] = {"count": len(collected), "ms": elapsed_ms}
        fused = _merge(fused, collected) if fused else list(collected)
        if _has_readable(fused):
            logger.info("空快照升级生效: %s 补回 %d 个元素", name, len(collected))
            break
    return fused


def observe(hwnd: int, include_icons: bool = True) -> Snapshot:
    """Observe a window with all enabled dimensions, fused by priority."""
    region = window_region(hwnd)
    title = win32._window_text(hwnd)
    snapshot = Snapshot(hwnd=hwnd, title=title, region=region)

    order = CONFIG.enabled_dimensions()
    # OCR on a large/complex window is the latency killer (~6s on Krita-class
    # apps). When UIA already returned a rich, reliable element set, OCR is
    # mostly redundant — skip it. Sparse-UIA cases (canvas/OCR-mode) still run
    # OCR. Gated by perception.ocr_skip_when_uia_rich.
    skip_ocr_if_rich = bool(CONFIG.get("fusion", "ocr_skip_when_uia_rich", default=True))
    uia_rich_threshold = int(CONFIG.get("fusion", "uia_rich_threshold", default=20))
    uia_count = 0
    fused: list[UIElement] = []
    for dim in order:
        started = time.monotonic()
        collected: list[UIElement] = []
        try:
            if dim == "uia":
                collected = uia.collect(hwnd)
                uia_count = sum(1 for e in collected if (e.text or "").strip())
            elif dim == "win32":
                collected = win32.collect(hwnd)
                collected.extend(win32.explorer_items(hwnd))
            elif dim == "ocr":
                if skip_ocr_if_rich and uia_count >= uia_rich_threshold:
                    snapshot.dim_stats["ocr"] = {"count": 0, "ms": 0, "skipped": "uia_rich"}
                    continue
                collected = ocr.collect(region)
            elif dim == "cv":
                collected = vision_cv.detect_icon_candidates(region) if include_icons else []
            elif dim == "vlm":
                collected = vlm.collect(region)
        except Exception as exc:
            logger.warning("感知维度 %s 异常: %s", dim, exc)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        snapshot.dim_stats[dim] = {"count": len(collected), "ms": elapsed_ms}
        fused = _merge(fused, collected) if fused else list(collected)

    # CV icon candidates that overlap any textual element are noise — drop them.
    fused = [
        element for element in fused
        if not (
            element.source == "cv"
            and element.role == "icon"
            and any(
                other.source != "cv" and element.iou(other) > 0.25 for other in fused
            )
        )
    ]

    # Phase 2-D: never hand the agent a blind (no readable element) snapshot
    # without trying the rescue ladder first.
    if (
        not _has_readable(fused)
        and CONFIG.get("fusion", "escalate_on_empty", default=True)
    ):
        fused = _escalate_empty(snapshot, region, fused)

    for idx, element in enumerate(fused, start=1):
        element.elem_id = idx
    snapshot.elements = fused

    BUS.publish(
        "perception",
        hwnd=hwnd,
        title=title,
        dims=snapshot.dim_stats,
        total=len(fused),
    )
    return snapshot


def observe_screen() -> list[dict]:
    """Top-level window list (independent of dimension toggles)."""
    return win32.list_windows()
