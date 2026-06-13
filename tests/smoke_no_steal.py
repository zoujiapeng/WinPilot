"""Smoke test for no-mouse-steal mode: clicks/scrolls must not move the
physical cursor when the experimental mode is on (default off)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32api  # noqa: E402
import win32gui  # noqa: E402

from winpilot.action import executor  # noqa: E402
from winpilot.config import CONFIG  # noqa: E402


def _set_mode(on: bool) -> None:
    CONFIG._data["agent"]["no_mouse_steal"] = on  # noqa: SLF001 (test)


def test_no_steal_click_keeps_cursor() -> None:
    subprocess.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
    subprocess.Popen(["notepad"])
    time.sleep(2)
    hwnd = win32gui.GetForegroundWindow()
    rect = win32gui.GetWindowRect(hwnd)
    target = (rect[0] + 200, rect[1] + 120)  # somewhere inside notepad

    # park the user's cursor far from the click target
    parked = (60, 60)
    win32api.SetCursorPos(parked)
    time.sleep(0.2)

    _set_mode(True)
    r = executor.click(target[0], target[1], hwnd=hwnd)
    time.sleep(0.2)
    after = win32api.GetCursorPos()
    moved = abs(after[0] - parked[0]) + abs(after[1] - parked[1])
    print(f"[no-steal] click via={r.get('via')} | 光标 {parked}→{after} 位移={moved}px")
    assert moved <= 3, f"不夺光标模式下光标不应移动，却移了 {moved}px"
    assert r.get("via") in ("post_message", "sendinput_restored")
    print("[OK] 不夺光标: 点击后物理光标留在原处")

    # control: with mode OFF, a click DOES move the cursor (normal behavior)
    _set_mode(False)
    win32api.SetCursorPos(parked)
    time.sleep(0.2)
    executor.click(target[0], target[1], hwnd=hwnd)
    time.sleep(0.2)
    after2 = win32api.GetCursorPos()
    moved2 = abs(after2[0] - target[0]) + abs(after2[1] - target[1])
    print(f"[normal] 光标移到 {after2} (目标{target}) 距离={moved2}px")
    assert moved2 <= 5, "普通模式光标应移到点击点"
    print("[OK] 普通模式: 点击照常移动光标(对照)")

    subprocess.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
    _set_mode(False)


if __name__ == "__main__":
    try:
        test_no_steal_click_keeps_cursor()
        print("\n不夺光标模式 smoke 通过 ✓")
    finally:
        _set_mode(False)
