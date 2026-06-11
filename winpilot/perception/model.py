"""Shared perception data model: UIElement, the unit all dimensions emit."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIElement:
    """A perceived on-screen element, source-agnostic.

    Coordinates are screen-space physical pixels (center point + bbox).
    """

    elem_id: int = -1                # assigned by fusion, stable within a turn
    source: str = ""                 # uia | win32 | ocr | cv | vlm
    role: str = ""                   # button / edit / text / icon / window ...
    text: str = ""
    cx: int = 0
    cy: int = 0
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    states: list[str] = field(default_factory=list)   # enabled/disabled/checked/focused/offscreen...
    confidence: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)  # automation_id, class_name, runtime handle...

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def iou(self, other: "UIElement") -> float:
        ax1, ay1, ax2, ay2 = self.bbox
        bx1, by1, bx2, by2 = other.bbox
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = self.width * self.height + other.width * other.height - inter
        return inter / union if union > 0 else 0.0

    def to_line(self) -> str:
        """Compact one-line text representation for the LLM."""
        parts = [f"[{self.elem_id}]", self.role or "elem"]
        if self.text:
            text = self.text if len(self.text) <= 60 else self.text[:57] + "..."
            parts.append(f'"{text}"')
        parts.append(f"({self.cx},{self.cy})")
        if self.states:
            parts.append(",".join(self.states))
        parts.append(f"src={self.source}")
        if self.source == "ocr" and self.confidence < 1.0:
            parts.append(f"conf={self.confidence:.2f}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "elem_id": self.elem_id,
            "source": self.source,
            "role": self.role,
            "text": self.text,
            "cx": self.cx,
            "cy": self.cy,
            "bbox": [self.left, self.top, self.width, self.height],
            "states": list(self.states),
            "confidence": round(self.confidence, 3),
        }
