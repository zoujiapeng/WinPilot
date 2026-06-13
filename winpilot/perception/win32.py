"""Win32 dimension: window enumeration, classic-control text, Explorer items.

Three capabilities:
1. ``list_windows`` — alt-tab style top-level window list (always available,
   used by the agent's list_windows tool regardless of toggles).
2. ``collect`` — classic Win32 child controls via EnumChildWindows +
   WM_GETTEXT; catches legacy apps whose UIA providers are poor.
3. ``explorer_items`` — files in an open Explorer window via Shell COM,
   no OCR/UIA needed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

import win32con
import win32gui
import win32process

from ..utils.log import logger
from .model import UIElement

_CLASS_ROLE = {
    "Button": "button",
    "Edit": "edit",
    "Static": "text",
    "ComboBox": "combobox",
    "ListBox": "list",
    "SysListView32": "list",
    "SysTreeView32": "tree",
    "ToolbarWindow32": "toolbar",
    "msctls_statusbar32": "statusbar",
    "RICHEDIT50W": "edit",
    "RichEdit20W": "edit",
}

user32 = ctypes.windll.user32


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _send_wm_gettext(hwnd: int, max_len: int = 4096) -> str:
    """WM_GETTEXT with timeout — works cross-process for classic controls."""
    buf = ctypes.create_unicode_buffer(max_len)
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        hwnd, win32con.WM_GETTEXT, max_len, buf,
        win32con.SMTO_ABORTIFHUNG, 200, ctypes.byref(result),
    )
    return buf.value if ok else ""


def list_windows() -> list[dict]:
    """Visible top-level windows with title, ordered by z-order."""
    windows: list[dict] = []

    def callback(hwnd: int, _param) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        title = _window_text(hwnd)
        if not title:
            return True
        # skip tool windows / our own overlay-style windows
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex_style & win32con.WS_EX_TOOLWINDOW:
            return True
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] - rect[0] < 50 or rect[3] - rect[1] < 50:
            return True
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        windows.append(
            {
                "hwnd": hwnd,
                "title": title,
                "class_name": win32gui.GetClassName(hwnd),
                "rect": list(rect),
                "pid": pid,
            }
        )
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception as exc:
        logger.warning("Win32: EnumWindows 失败: %s", exc)
    return windows


def find_window(title_substr: str) -> dict | None:
    """First visible window whose title contains the substring (case-insensitive).

    Alias-aware: a query like "计算器" also matches a window titled "Calculator",
    so verify on English-titled built-in apps works.
    """
    from .app_aliases import expand

    needles = expand(title_substr) or [title_substr.lower()]
    windows = list_windows()
    for needle in needles:  # prefer the literal query first (expand keeps it)
        for win in windows:
            if needle in win["title"].lower():
                return win
    return None


# ------------------------------------------------------------- process info
def _process_name(pid: int) -> str:
    """Executable basename of a pid ('' on access denied)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_app(name_substr: str) -> dict:
    """Is an app running — even with no visible window (tray-resident)?

    Scans all processes by image name, then ALL top-level windows (hidden
    included) belonging to those pids. Lets the agent distinguish
    "not running" from "running but hidden in the tray" before launching.
    """
    needle = name_substr.lower().removesuffix(".exe")
    pids: list[int] = []
    # Toolhelp snapshot: lightweight full-process walk, no extra deps.
    TH32CS_SNAPPROCESS = 0x2

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot != -1:
        try:
            entry = _PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    exe = entry.szExeFile.lower().removesuffix(".exe")
                    if needle in exe:
                        pids.append(int(entry.th32ProcessID))
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)

    windows: list[dict] = []
    if pids:
        pid_set = set(pids)

        def callback(hwnd: int, _param) -> bool:
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in pid_set:
                    title = _window_text(hwnd)
                    visible = bool(win32gui.IsWindowVisible(hwnd))
                    if title or visible:
                        windows.append(
                            {"hwnd": hwnd, "title": title, "visible": visible,
                             "pid": pid})
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(callback, None)
        except Exception as exc:
            logger.warning("Win32: find_app EnumWindows 失败: %s", exc)

    return {
        "running": bool(pids),
        "process_count": len(pids),
        "pids": pids[:20],
        "windows": windows[:20],
        "has_visible_window": any(w["visible"] for w in windows),
    }


def collect(hwnd: int) -> list[UIElement]:
    """Classic Win32 child controls of a window. Never raises."""
    elements: list[UIElement] = []

    def callback(child: int, _param) -> bool:
        if len(elements) >= 200:
            return False
        try:
            if not user32.IsWindowVisible(child):
                return True
            class_name = win32gui.GetClassName(child)
            role = _CLASS_ROLE.get(class_name)
            if role is None:
                return True
            rect = win32gui.GetWindowRect(child)
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            if width <= 0 or height <= 0:
                return True
            text = _send_wm_gettext(child)
            if role == "edit" and len(text) > 200:
                text = text[:200] + "..."
            states = []
            if not user32.IsWindowEnabled(child):
                states.append("disabled")
            elements.append(
                UIElement(
                    source="win32",
                    role=role,
                    text=text.strip(),
                    cx=(rect[0] + rect[2]) // 2,
                    cy=(rect[1] + rect[3]) // 2,
                    left=rect[0],
                    top=rect[1],
                    width=width,
                    height=height,
                    states=states,
                    extra={"hwnd": child, "class_name": class_name},
                )
            )
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, callback, None)
    except Exception:
        pass  # EnumChildWindows raises if callback returns False — expected
    return elements


def explorer_items(hwnd: int) -> list[UIElement]:
    """Items of an Explorer window via Shell COM. Empty list if not Explorer."""
    try:
        import win32com.client

        shell = win32com.client.Dispatch("Shell.Application")
        for window in shell.Windows():
            try:
                if int(window.HWND) != hwnd:
                    continue
                items = window.Document.Folder.Items()
                elements = []
                for i, item in enumerate(items):
                    if i >= 200:
                        break
                    elements.append(
                        UIElement(
                            source="win32",
                            role="file",
                            text=item.Name,
                            extra={"path": item.Path},
                        )
                    )
                return elements
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Explorer COM 不可用: %s", exc)
    return []
