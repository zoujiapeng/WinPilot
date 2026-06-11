"""Screen capture utilities: full-screen / window-region capture via mss.

Process is set DPI-aware at import time so all coordinates from UIA, Win32
and mss agree (physical pixels).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
from dataclasses import dataclass

import mss
import numpy as np

# Per-Monitor v2 DPI awareness — must happen before any window/screen query.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

_local = threading.local()


def _get_mss() -> mss.mss:
    """mss instances are not thread-safe; keep one per thread."""
    if not hasattr(_local, "sct"):
        _local.sct = mss.mss()
    return _local.sct


@dataclass(frozen=True)
class Region:
    """Screen-space rectangle in physical pixels."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def clamp_to_screen(self) -> "Region":
        sw, sh = screen_size()
        left = max(0, min(self.left, sw - 1))
        top = max(0, min(self.top, sh - 1))
        right = max(left + 1, min(self.right, sw))
        bottom = max(top + 1, min(self.bottom, sh))
        return Region(left, top, right - left, bottom - top)


def screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def capture(region: Region | None = None) -> np.ndarray:
    """Capture screen (or region) as BGR ndarray (OpenCV convention)."""
    sct = _get_mss()
    if region is None:
        monitor = sct.monitors[0]
    else:
        region = region.clamp_to_screen()
        monitor = {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
    shot = sct.grab(monitor)
    frame = np.asarray(shot, dtype=np.uint8)  # BGRA
    return frame[:, :, :3].copy()  # BGR


def window_region(hwnd: int) -> Region:
    """Window rectangle in physical pixels, DWM-accurate (no invisible borders)."""
    rect = ctypes.wintypes.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if res != 0:  # fall back to GetWindowRect
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError(f"GetWindowRect 失败 hwnd={hwnd}")
    return Region(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
