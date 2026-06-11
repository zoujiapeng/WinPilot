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
            max_tokens=512,
            temperature=0.1,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("VLM describe 失败: %s", exc)
        return f"VLM 调用失败: {exc}"


_GROUND_PROMPT = (
    "List the interactive UI elements visible in this screenshot. "
    "Reply ONLY with a JSON array, each item: "
    '{"role": "button|edit|text|icon|link|image", "text": "...", '
    '"x": <center-x-px>, "y": <center-y-px>}. Coordinates are pixels in this image.'
)


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
            max_tokens=1500,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "[]"
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        items = json.loads(raw[start : end + 1])
        elements = []
        for item in items[:80]:
            try:
                x = int(int(item["x"]) / scale) + region.left
                y = int(int(item["y"]) / scale) + region.top
                elements.append(
                    UIElement(
                        source="vlm",
                        role=str(item.get("role", "elem")),
                        text=str(item.get("text", "")),
                        cx=x, cy=y, left=x - 8, top=y - 8, width=16, height=16,
                        confidence=0.7,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return elements
    except Exception as exc:
        logger.warning("VLM collect 失败: %s", exc)
        return []
