"""System-tray dimension: enumerate and click notification-area icons.

Apps "hidden in the tray" (QQ, WeChat, cloud drives...) have no visible
top-level window, so ``list_windows`` can't see them. This module walks the
taskbar's UIA tree (Shell_TrayWnd) and — when needed — expands the Win11
overflow flyout ("显示隐藏的图标" chevron) to reach hidden icons.
"""
from __future__ import annotations

import time

import uiautomation as auto
import win32gui

from ..utils.log import logger
from .model import UIElement

_OVERFLOW_TITLES = ("系统托盘溢出窗口", "System tray overflow window")
_CHEVRON_NAMES = ("显示隐藏的图标", "Show Hidden Icons", "通知 V 形", "Notification Chevron")
_MAX_DEPTH = 8


def _walk_buttons(root: auto.Control, source_tag: str) -> list[UIElement]:
    """Collect every named button under a UIA root (bounded depth)."""
    found: list[UIElement] = []

    def walk(ctrl: auto.Control, depth: int) -> None:
        if depth > _MAX_DEPTH or len(found) >= 60:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            try:
                name = (child.Name or "").strip()
                if child.ControlTypeName == "ButtonControl" and name:
                    rect = child.BoundingRectangle
                    if rect.width() > 0 and rect.height() > 0:
                        found.append(
                            UIElement(
                                source="uia",
                                role="tray-icon",
                                text=name,
                                cx=(rect.left + rect.right) // 2,
                                cy=(rect.top + rect.bottom) // 2,
                                left=rect.left,
                                top=rect.top,
                                width=rect.width(),
                                height=rect.height(),
                                extra={"uia_ctrl": child, "area": source_tag},
                            )
                        )
                walk(child, depth + 1)
            except Exception:
                continue

    walk(root, 0)
    return found


def _taskbar_icons() -> list[UIElement]:
    hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
    if not hwnd:
        return []
    try:
        return _walk_buttons(auto.ControlFromHandle(hwnd), "taskbar")
    except Exception as exc:
        logger.warning("托盘: 任务栏遍历失败: %s", exc)
        return []


def _find_overflow_window() -> int:
    """hwnd of the opened overflow flyout, else 0."""
    result = 0

    def callback(hwnd: int, _param) -> bool:
        nonlocal result
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if any(t in title for t in _OVERFLOW_TITLES):
                result = hwnd
                return False
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass  # EnumWindows raises when callback returns False — expected
    return result


def _open_overflow(taskbar_icons: list[UIElement]) -> int:
    """Click the chevron to open the hidden-icons flyout. Returns its hwnd."""
    existing = _find_overflow_window()
    if existing:
        return existing
    chevron = next(
        (icon for icon in taskbar_icons
         if any(n in icon.text for n in _CHEVRON_NAMES)),
        None,
    )
    if chevron is None:
        return 0
    from ..action import executor
    executor.click(chevron.cx, chevron.cy)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        hwnd = _find_overflow_window()
        if hwnd:
            time.sleep(0.25)  # let icons render
            return hwnd
        time.sleep(0.15)
    return 0


def list_tray_icons(expand_overflow: bool = True) -> list[UIElement]:
    """All tray icons: visible taskbar area + (optionally) overflow flyout.

    Opening the overflow flyout is a visible UI action; the flyout closes
    itself on the next click elsewhere. The Win11 XAML taskbar builds its
    UIA tree lazily — an empty first walk warrants one retry.
    """
    icons = _taskbar_icons()
    if not icons:
        time.sleep(0.4)  # XAML taskbar UIA tree may still be materializing
        icons = _taskbar_icons()
    if expand_overflow:
        overflow_hwnd = _open_overflow(icons)
        if overflow_hwnd:
            try:
                icons.extend(
                    _walk_buttons(auto.ControlFromHandle(overflow_hwnd), "overflow"))
            except Exception as exc:
                logger.warning("托盘: 溢出区遍历失败: %s", exc)
    return icons


def click_tray_icon(name_substr: str, double: bool = False,
                    button: str = "left") -> dict:
    """Find a tray icon by name substring and click it. Never raises."""
    needle = name_substr.lower()
    try:
        icons = list_tray_icons(expand_overflow=True)
    except Exception as exc:
        return {"ok": False, "error": f"托盘枚举失败: {exc}"}
    hit = next((i for i in icons if needle in i.text.lower()), None)
    if hit is None:
        names = [i.text for i in icons][:30]
        return {"ok": False, "error": f'托盘中未找到 "{name_substr}"',
                "available": names}
    from ..action import executor
    result = executor.click(hit.cx, hit.cy, button=button, double=double)
    result["icon"] = hit.text
    result["area"] = hit.extra.get("area", "")
    return result
