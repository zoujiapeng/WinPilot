"""VLM grounding precision gate (Phase 3-G).

Renders a synthetic UI screenshot with KNOWN element positions (Chinese +
English labels), asks the configured VLM to ground every element as a bbox,
and measures center-pixel error against ground truth.

Gate (computer-use viability): hit-rate — predicted center must fall INSIDE
the true element rect (i.e. a click would land) for >= 70% of elements, and
mean center error <= 25 px. Below that, VLM stays an auxiliary describe tool.

Usage:
    python tests/bench_vlm_grounding.py [model ...]
Defaults to the vision-capable candidates: mimo-v2.5 mimo-v2-omni
"""
from __future__ import annotations

import base64
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.config import CONFIG  # noqa: E402

W, H = 1280, 720
# (label, left, top, width, height, kind)
ELEMENTS = [
    ("保存", 980, 640, 120, 44, "button"),
    ("取消", 1120, 640, 120, 44, "button"),
    ("打开文件", 40, 80, 140, 40, "button"),
    ("设置", 200, 80, 100, 40, "button"),
    ("Search", 360, 80, 220, 40, "edit"),
    ("用户名", 200, 220, 90, 30, "text"),
    ("立即登录", 420, 380, 160, 48, "button"),
    ("Remember me", 200, 300, 200, 28, "checkbox"),
]


def render_ui() -> np.ndarray:
    """Synthetic but realistic-looking dialog with known geometry."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (W, H), (243, 243, 243))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 36], fill=(0, 99, 177))  # title bar
    font_path = "C:/Windows/Fonts/msyh.ttc"
    font = ImageFont.truetype(font_path, 18)
    title_font = ImageFont.truetype(font_path, 16)
    draw.text((12, 8), "示例应用 - 设置中心", font=title_font, fill=(255, 255, 255))

    for label, x, y, w, h, kind in ELEMENTS:
        cx, cy = x + w // 2, y + h // 2
        if kind == "button":
            draw.rounded_rectangle([x, y, x + w, y + h], radius=6,
                                   fill=(0, 120, 215), outline=(0, 84, 153))
            tb = draw.textbbox((0, 0), label, font=font)
            draw.text((cx - (tb[2] - tb[0]) // 2, cy - (tb[3] - tb[1]) // 2 - 2),
                      label, font=font, fill=(255, 255, 255))
        elif kind == "edit":
            draw.rectangle([x, y, x + w, y + h], fill=(255, 255, 255),
                           outline=(120, 120, 120), width=2)
            draw.text((x + 8, y + 8), label, font=font, fill=(150, 150, 150))
        elif kind == "checkbox":
            draw.rectangle([x, y + 4, x + 20, y + 24], outline=(90, 90, 90),
                           width=2, fill=(255, 255, 255))
            draw.text((x + 28, y), label, font=font, fill=(40, 40, 40))
        else:  # text
            draw.text((x, y), label, font=font, fill=(40, 40, 40))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


_PROMPT = (
    "Locate every interactive UI element and text label in this screenshot. "
    "Reply ONLY with a JSON array; each item: "
    '{"text": "<element text>", "box": [x1, y1, x2, y2]} '
    "where coordinates are NORMALIZED to 0-1000 (top-left origin, "
    "x1000 = right/bottom edge of the image)."
)


def _decode_box(box: list, img_w: int, img_h: int) -> tuple[float, float]:
    """Center of a box in image pixels.

    MiMo (Qwen2.5-VL lineage) natively grounds on a 0-1000 normalized grid —
    confirmed empirically: raw outputs were exactly true*(1000/W), true*(1000/H).
    Values > 1000 are treated as already-pixel coordinates.
    """
    x1, y1, x2, y2 = (float(v) for v in box)
    if max(x1, y1, x2, y2) <= 1000.0:
        x1, x2 = x1 / 1000 * img_w, x2 / 1000 * img_w
        y1, y2 = y1 / 1000 * img_h, y2 / 1000 * img_h
    return (x1 + x2) / 2, (y1 + y2) / 2


def bench(model: str, b64: str, thinking: bool = True) -> dict:
    from openai import OpenAI

    vlm_cfg = CONFIG.get("vlm", default={})
    client = OpenAI(base_url=vlm_cfg["base_url"], api_key=vlm_cfg["api_key"],
                    timeout=120)
    extra = {} if thinking else {"extra_body": {"thinking": {"type": "disabled"}}}
    resp = client.chat.completions.create(
        model=model, max_tokens=2000, temperature=0.0,
        messages=[{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": _PROMPT},
        ]}],
        **extra,
    )
    raw = resp.choices[0].message.content or "[]"
    start, end = raw.find("["), raw.rfind("]")
    items = json.loads(raw[start:end + 1]) if 0 <= start < end else []

    hits, errors, found = 0, [], 0
    rows = []
    for label, x, y, w, h, _kind in ELEMENTS:
        gt_cx, gt_cy = x + w // 2, y + h // 2
        best = None
        for item in items:
            text = str(item.get("text", ""))
            if label.lower() in text.lower() or text.lower() in label.lower():
                box = item.get("box") or []
                if len(box) == 4:
                    px, py = _decode_box(box, W, H)
                    err = ((px - gt_cx) ** 2 + (py - gt_cy) ** 2) ** 0.5
                    if best is None or err < best[0]:
                        best = (err, px, py)
        if best is None:
            rows.append(f"  ✗ \"{label}\" 未找到")
            continue
        found += 1
        err, px, py = best
        inside = x <= px <= x + w and y <= py <= y + h
        hits += inside
        errors.append(err)
        rows.append(f'  {"✓" if inside else "≈"} "{label}" 误差{err:.0f}px'
                    f' 预测({px:.0f},{py:.0f}) 真值({gt_cx},{gt_cy})')
    n = len(ELEMENTS)
    mean_err = sum(errors) / len(errors) if errors else 9999
    result = {
        "model": model, "found": found, "total": n, "hits": hits,
        "mean_err": round(mean_err, 1),
        "hit_rate": round(hits / n, 2),
        "pass": hits / n >= 0.7 and mean_err <= 25,
    }
    print(f"\n=== {model} ===")
    print("\n".join(rows))
    print(f"找到 {found}/{n} | 点击命中(中心落在元素内) {hits}/{n} "
          f"| 平均误差 {mean_err:.1f}px | 门禁: {'PASS ✓' if result['pass'] else 'FAIL ✗'}")
    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    thinking = "--no-thinking" not in args
    models = [a for a in args if not a.startswith("--")] or ["mimo-v2.5", "mimo-v2-omni"]
    image = render_ui()
    cv2.imwrite(os.path.join(os.path.dirname(__file__), "bench_ui.png"), image)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf.tobytes()).decode()
    print(f"thinking={'on' if thinking else 'off'}")
    results = [bench(m, b64, thinking=thinking) for m in models]
    print("\n汇总:", json.dumps(results, ensure_ascii=False))
