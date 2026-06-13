"""Optional VLM dimension: when the user configures a multimodal model
(MiMo / Qwen2.5-VL / GPT-4o-class via any OpenAI-compatible endpoint),
WinPilot gains true vision:

- ``describe(region, question)`` — ad-hoc visual Q&A about a screen region.
- ``collect(region)`` — VLM-grounded element extraction (slowest dimension,
  lowest default priority; used when structured dimensions fail).
"""
from __future__ import annotations

import base64
import json

import cv2

from ..config import CONFIG
from ..utils.log import logger
from ..utils.screenshot import Region, capture
from .model import UIElement


def is_configured() -> bool:
    vlm = CONFIG.get("vlm", default={})
    return bool(vlm.get("enabled") and vlm.get("base_url") and vlm.get("model"))


def _client():
    from openai import OpenAI

    vlm = CONFIG.get("vlm", default={})
    return OpenAI(
        base_url=vlm["base_url"],
        api_key=vlm.get("api_key") or "none",
        timeout=120,
    )


def _encode_region(region: Region, max_side: int = 1280) -> tuple[str, float]:
    """JPEG-base64 of a region; returns (b64, scale_applied)."""
    image = capture(region.clamp_to_screen())
    h, w = image.shape[:2]
    scale = 1.0
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        image = cv2.resize(image, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(buf.tobytes()).decode(), scale


def _extra_body() -> dict:
    """MiMo-style reasoning models burn the whole token budget on thinking
    before emitting JSON — disable it unless explicitly turned on
    (benchmarked: grounding precision is equal-or-better without thinking).
    """
    if CONFIG.get("vlm", "thinking", default=False):
        return {}
    return {"thinking": {"type": "disabled"}}


def describe(region: Region, question: str) -> str:
    """Ask the configured VLM a question about a screen region."""
    if not is_configured():
        return "VLM 未配置"
    b64, _scale = _encode_region(region)
    vlm = CONFIG.get("vlm", default={})
    try:
        resp = _client().chat.completions.create(
            model=vlm["model"],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": question},
                    ],
                }
            ],
            max_tokens=800,
            temperature=0.1,
            extra_body=_extra_body(),
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("VLM describe 失败: %s", exc)
        return f"VLM 调用失败: {exc}"


_GROUND_PROMPT = (
    "Locate every interactive UI element and text label in this screenshot. "
    "Reply ONLY with a JSON array; each item: "
    '{"role": "button|edit|text|icon|link|checkbox|image", "text": "<text>", '
    '"box": [x1, y1, x2, y2]} '
    "where coordinates are NORMALIZED to 0-1000 (top-left origin, "
    "1000 = right/bottom edge of the image)."
)


def _decode_box(box: list, region: Region, scale: float) -> tuple[int, int, int, int]:
    """Box -> absolute screen rect (left, top, width, height).

    MiMo / Qwen2.5-VL lineage natively grounds on a 0-1000 normalized grid
    (verified by benchmark: mean error ~6px once decoded). Normalized coords
    map directly onto the region — independent of the JPEG downscale. Values
    > 1000 are treated as pixels in the submitted (possibly scaled) image.
    """
    x1, y1, x2, y2 = (float(v) for v in box)
    if max(x1, y1, x2, y2) <= 1000.0:
        left = region.left + x1 / 1000 * region.width
        top = region.top + y1 / 1000 * region.height
        width = (x2 - x1) / 1000 * region.width
        height = (y2 - y1) / 1000 * region.height
    else:
        left = region.left + x1 / scale
        top = region.top + y1 / scale
        width = (x2 - x1) / scale
        height = (y2 - y1) / scale
    return int(left), int(top), max(1, int(width)), max(1, int(height))


def collect(region: Region) -> list[UIElement]:
    """VLM-grounded element list for a region. Never raises."""
    if not is_configured():
        return []
    try:
        b64, scale = _encode_region(region)
        vlm = CONFIG.get("vlm", default={})
        resp = _client().chat.completions.create(
            model=vlm["model"],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": _GROUND_PROMPT},
                    ],
                }
            ],
            max_tokens=3000,
            temperature=0.0,
            extra_body=_extra_body(),
        )
        raw = resp.choices[0].message.content or "[]"
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        items = json.loads(raw[start : end + 1])
        elements = []
        for item in items[:80]:
            try:
                box = item.get("box") or []
                if len(box) != 4:
                    # Legacy single-point answers: keep a small clickable box.
                    x = int(int(item["x"]) / scale) + region.left
                    y = int(int(item["y"]) / scale) + region.top
                    left, top, width, height = x - 8, y - 8, 16, 16
                else:
                    left, top, width, height = _decode_box(box, region, scale)
                elements.append(
                    UIElement(
                        source="vlm",
                        role=str(item.get("role", "elem")),
                        text=str(item.get("text", "")),
                        cx=left + width // 2,
                        cy=top + height // 2,
                        left=left, top=top, width=width, height=height,
                        confidence=0.8,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return elements
    except Exception as exc:
        logger.warning("VLM collect 失败: %s", exc)
        return []
