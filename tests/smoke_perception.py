"""Smoke test for Phase 2-D perception hardening.

- OCR coordinate mapping with a synthetic image (origin offset must hold)
- dynamic min_confidence override
- template save -> list -> match round-trip on the live screen
- empty-snapshot escalation config wiring
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.config import CONFIG  # noqa: E402
from winpilot.perception import fusion, ocr, vision_cv  # noqa: E402
from winpilot.utils.screenshot import Region, capture  # noqa: E402


def test_ocr_origin_mapping() -> None:
    """Rendered text at a known pixel position must map through origin offset."""
    img = np.full((120, 420, 3), 255, dtype=np.uint8)
    cv2.putText(img, "HELLO 12345", (20, 70), cv2.FONT_HERSHEY_SIMPLEX,
                1.4, (0, 0, 0), 3)
    origin = (1000, 2000)
    found = ocr.recognize(img, origin=origin)
    assert found, "合成图 OCR 应识别出文字"
    e = found[0]
    assert "HELLO" in e.text.upper().replace(" ", "") or "12345" in e.text
    assert e.left >= origin[0] and e.top >= origin[1], "坐标必须叠加 origin 偏移"
    assert e.left - origin[0] < 420 and e.top - origin[1] < 120
    print(f"[OK] OCR origin 映射: \"{e.text}\" at ({e.cx},{e.cy})")


def test_ocr_dynamic_conf() -> None:
    img = np.full((80, 300, 3), 255, dtype=np.uint8)
    cv2.putText(img, "TEST", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    strict = ocr.recognize(img, min_conf=0.99)
    loose = ocr.recognize(img, min_conf=0.1)
    assert len(loose) >= len(strict), "低阈值不应比高阈值识别得更少"
    print(f"[OK] 动态置信度: conf0.99={len(strict)}条 conf0.1={len(loose)}条")


def test_template_roundtrip() -> None:
    """save_template a live-screen patch, then find_image must locate it."""
    region = Region(100, 100, 80, 80)
    path = vision_cv.save_template(region, "smoketest_tpl")
    try:
        assert "smoketest_tpl" in vision_cv.list_templates()
        matches = vision_cv.match_template("smoketest_tpl")
        assert matches, "刚截取的屏幕区域应能立即在屏幕上匹配到"
        best = matches[0]
        dist = abs(best.cx - 140) + abs(best.cy - 140)
        assert dist <= 12, f"匹配位置应在截取处附近，偏差 {dist}px"
        print(f"[OK] 模板往返: 保存→列出→匹配 中心({best.cx},{best.cy}) "
              f"置信度{best.confidence:.2f}")
    finally:
        path.unlink(missing_ok=True)


def test_escalation_wiring() -> None:
    assert CONFIG.get("fusion", "escalate_on_empty", default=None) is not None
    assert fusion._has_readable([]) is False

    class _E:  # minimal stand-in
        text = "x"
    assert fusion._has_readable([_E()]) is True
    # full-screen OCR fallback should produce *something* on a live desktop
    elems = ocr.collect(Region(0, 0, 800, 400), min_conf=0.4)
    print(f"[OK] 升级链路配置就绪 (全屏OCR采样: {len(elems)}条)")


if __name__ == "__main__":
    test_ocr_origin_mapping()
    test_ocr_dynamic_conf()
    test_template_roundtrip()
    test_escalation_wiring()
    print("\n感知加固 smoke 全部通过 ✓")
