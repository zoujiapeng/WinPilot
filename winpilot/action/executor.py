"""Action executor: SendInput-based mouse/keyboard plus window management.

All actions are physical-pixel screen coordinates. ``type_text`` uses
KEYEVENTF_UNICODE so Chinese/emoji input works without an IME. A UIA fast
path (Invoke/SetValue) is attempted first when a live UIA handle is present
on the resolved element — faster and more reliable than synthetic clicks.
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui

from ..config import CONFIG
from ..utils.log import BUS, logger

user32 = ctypes.windll.user32

# ---------------------------------------------------------------- SendInput
_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_UNICODE, _KEYEVENTF_KEYUP, _KEYEVENTF_EXTENDEDKEY = 0x4, 0x2, 0x1


def _send_inputs(inputs: list[_Input]) -> None:
    array = (_Input * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
    if sent != len(inputs):
        raise OSError(f"SendInput 只发送了 {sent}/{len(inputs)}")


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    inp = _Input()
    inp.type = _INPUT_KEYBOARD
    inp.union.ki = _KeyBdInput(vk, scan, flags, 0, ctypes.pointer(ctypes.c_ulong(0)))
    return inp


def _mouse_input(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> _Input:
    inp = _Input()
    inp.type = _INPUT_MOUSE
    inp.union.mi = _MouseInput(dx, dy, data, flags, 0, ctypes.pointer(ctypes.c_ulong(0)))
    return inp


# ----------------------------------------------------------------- VK table
_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "capslock": 0x14, "numlock": 0x90, "printscreen": 0x2C, "menu": 0x5D,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
}
_EXTENDED_VKS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def _vk_of(key: str) -> int:
    key = key.strip().lower()
    if key in _VK:
        return _VK[key]
    if len(key) == 1:
        # VkKeyScanW maps a character to VK (low byte)
        res = user32.VkKeyScanW(ctypes.c_wchar(key))
        if res != -1:
            return res & 0xFF
    raise ValueError(f"未知按键: {key}")


def _dry_run() -> bool:
    return bool(CONFIG.get("agent", "dry_run", default=False))


# ---------------------------------------------------------------- mouse API
def move_mouse(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def click(x: int, y: int, button: str = "left", double: bool = False, hwnd: int | None = None) -> dict:
    """Click at screen coords. Brings target window forward first if given."""
    if _dry_run():
        return {"ok": True, "dry_run": True}
    if hwnd:
        focus_window(hwnd)
    move_mouse(x, y)
    time.sleep(0.05)
    if button == "left":
        down, up = win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP
    elif button == "right":
        down, up = win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        down, up = win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP
    else:
        raise ValueError(f"未知鼠标键: {button}")
    repeats = 2 if double else 1
    for _ in range(repeats):
        _send_inputs([_mouse_input(down), _mouse_input(up)])
        if double:
            time.sleep(0.06)
    BUS.publish("action", action="click", x=x, y=y, button=button, double=double)
    return {"ok": True, "x": x, "y": y}


def drag(x1: int, y1: int, x2: int, y2: int, duration_s: float = 0.4) -> dict:
    if _dry_run():
        return {"ok": True, "dry_run": True}
    move_mouse(x1, y1)
    time.sleep(0.08)
    _send_inputs([_mouse_input(win32con.MOUSEEVENTF_LEFTDOWN)])
    steps = max(8, int(duration_s / 0.02))
    for i in range(1, steps + 1):
        move_mouse(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
        time.sleep(duration_s / steps)
    _send_inputs([_mouse_input(win32con.MOUSEEVENTF_LEFTUP)])
    BUS.publish("action", action="drag", frm=[x1, y1], to=[x2, y2])
    return {"ok": True}


def scroll(x: int, y: int, amount: int) -> dict:
    """amount: positive=up, negative=down; in notches."""
    if _dry_run():
        return {"ok": True, "dry_run": True}
    move_mouse(x, y)
    time.sleep(0.04)
    _send_inputs([_mouse_input(win32con.MOUSEEVENTF_WHEEL, data=amount * 120 & 0xFFFFFFFF)])
    BUS.publish("action", action="scroll", x=x, y=y, amount=amount)
    return {"ok": True}


# ------------------------------------------------------------- keyboard API
def type_text(text: str, interval_s: float = 0.008) -> dict:
    """Unicode injection — IME-independent, supports Chinese."""
    if _dry_run():
        return {"ok": True, "dry_run": True}
    for char in text:
        if char == "\n":
            press_keys(["enter"])
            continue
        code = ord(char)
        if code > 0xFFFF:  # surrogate pair for emoji etc.
            code -= 0x10000
            pair = (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
        else:
            pair = (code,)
        for unit in pair:
            _send_inputs([
                _key_input(scan=unit, flags=_KEYEVENTF_UNICODE),
                _key_input(scan=unit, flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP),
            ])
        time.sleep(interval_s)
    BUS.publish("action", action="type_text", length=len(text))
    return {"ok": True, "typed": len(text)}


def press_keys(keys: list[str]) -> dict:
    """Press a chord, e.g. ["ctrl","s"] or single ["enter"]."""
    if _dry_run():
        return {"ok": True, "dry_run": True}
    vks = [_vk_of(k) for k in keys]
    downs, ups = [], []
    for vk in vks:
        flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
        downs.append(_key_input(vk=vk, flags=flags))
        ups.append(_key_input(vk=vk, flags=flags | _KEYEVENTF_KEYUP))
    _send_inputs(downs)
    time.sleep(0.03)
    _send_inputs(list(reversed(ups)))
    BUS.publish("action", action="hotkey", keys=keys)
    return {"ok": True, "keys": keys}


# --------------------------------------------------------------- window API
def focus_window(hwnd: int) -> dict:
    """Robust foreground switch (works around SetForegroundWindow locks)."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.15)
        if win32gui.GetForegroundWindow() == hwnd:
            return {"ok": True}
        # ALT tap trick: unlocks SetForegroundWindow for our process
        _send_inputs([
            _key_input(vk=0x12), _key_input(vk=0x12, flags=_KEYEVENTF_KEYUP),
        ])
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        ok = win32gui.GetForegroundWindow() == hwnd
        if not ok:
            win32gui.BringWindowToTop(hwnd)
            time.sleep(0.1)
            ok = win32gui.GetForegroundWindow() == hwnd
        return {"ok": ok}
    except Exception as exc:
        logger.warning("focus_window 失败 hwnd=%s: %s", hwnd, exc)
        return {"ok": False, "error": str(exc)}


def launch(command: str) -> dict:
    """Launch an app: exe name, full path, or shell command."""
    if _dry_run():
        return {"ok": True, "dry_run": True}
    try:
        subprocess.Popen(command, shell=True)
        BUS.publish("action", action="launch", command=command)
        return {"ok": True, "command": command}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def close_window(hwnd: int) -> dict:
    if _dry_run():
        return {"ok": True, "dry_run": True}
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------ UIA fast path
def uia_invoke(element) -> bool:
    """Invoke/Toggle via UIA pattern if the element carries a live handle."""
    ctrl = element.extra.get("uia_ctrl")
    if ctrl is None:
        return False
    for getter in ("GetInvokePattern", "GetTogglePattern", "GetSelectionItemPattern"):
        try:
            pattern = getattr(ctrl, getter)()
            if pattern:
                if getter == "GetInvokePattern":
                    pattern.Invoke()
                elif getter == "GetTogglePattern":
                    pattern.Toggle()
                else:
                    pattern.Select()
                return True
        except Exception:
            continue
    return False


def uia_set_value(element, value: str) -> bool:
    ctrl = element.extra.get("uia_ctrl")
    if ctrl is None:
        return False
    try:
        pattern = ctrl.GetValuePattern()
        if pattern:
            pattern.SetValue(value)
            return True
    except Exception:
        pass
    return False
