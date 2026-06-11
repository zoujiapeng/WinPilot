"""UIA dimension: walk the UI Automation tree of a window.

Covers modern apps (UWP/WinUI), WPF, WinForms, Qt and most Win32 apps that
expose UIA providers. Emits UIElement with role/text/coords/states and keeps
the live automation element handle in `extra` for fast Invoke/SetValue paths.
"""
from __future__ import annotations

import time

import uiautomation as auto

from ..utils.log import logger
from .model import UIElement

# Roles we surface to the model. Anything else is treated as container and recursed.
_INTERESTING = {
    "ButtonControl": "button",
    "SplitButtonControl": "button",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "ComboBoxControl": "combobox",
    "EditControl": "edit",
    "DocumentControl": "document",
    "TextControl": "text",
    "HyperlinkControl": "link",
    "MenuItemControl": "menuitem",
    "MenuBarControl": "menubar",
    "TabItemControl": "tab",
    "ListItemControl": "listitem",
    "TreeItemControl": "treeitem",
    "DataItemControl": "dataitem",
    "HeaderItemControl": "headeritem",
    "SliderControl": "slider",
    "SpinnerControl": "spinner",
    "StatusBarControl": "statusbar",
    "ToolBarControl": "toolbar",
    "ImageControl": "image",
    "TitleBarControl": "titlebar",
    "WindowControl": "window",
    "PaneControl": "pane",
    "GroupControl": "group",
    "ListControl": "list",
    "ScrollBarControl": "scrollbar",
}
# Containers we recurse into but only emit if they carry their own name.
_CONTAINER_ROLES = {"window", "pane", "group", "list", "toolbar", "menubar"}

_MAX_ELEMENTS = 300
_MAX_DEPTH = 18
_TIME_BUDGET_S = 3.0


def _states_of(ctrl: auto.Control) -> list[str]:
    states: list[str] = []
    try:
        if not ctrl.IsEnabled:
            states.append("disabled")
    except Exception:
        pass
    try:
        if ctrl.IsOffscreen:
            states.append("offscreen")
    except Exception:
        pass
    try:
        toggle = ctrl.GetTogglePattern()
        if toggle:
            states.append("checked" if toggle.ToggleState == 1 else "unchecked")
    except Exception:
        pass
    try:
        sel = ctrl.GetSelectionItemPattern()
        if sel and sel.IsSelected:
            states.append("selected")
    except Exception:
        pass
    try:
        if ctrl.HasKeyboardFocus:
            states.append("focused")
    except Exception:
        pass
    return states


def _value_of(ctrl: auto.Control) -> str:
    try:
        vp = ctrl.GetValuePattern()
        if vp and vp.Value:
            return str(vp.Value)
    except Exception:
        pass
    return ""


def collect(hwnd: int) -> list[UIElement]:
    """Collect interesting UIA elements of the window. Never raises."""
    started = time.monotonic()
    elements: list[UIElement] = []
    try:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return []
    except Exception as exc:
        logger.warning("UIA: ControlFromHandle 失败 hwnd=%s: %s", hwnd, exc)
        return []

    def walk(ctrl: auto.Control, depth: int) -> None:
        if depth > _MAX_DEPTH or len(elements) >= _MAX_ELEMENTS:
            return
        if time.monotonic() - started > _TIME_BUDGET_S:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for child in children:
            if len(elements) >= _MAX_ELEMENTS:
                return
            try:
                ctype = child.ControlTypeName
                role = _INTERESTING.get(ctype)
                rect = child.BoundingRectangle
                width, height = rect.width(), rect.height()
                name = (child.Name or "").strip()
                visible = width > 0 and height > 0
                if role and visible:
                    value = _value_of(child)
                    text = name or value
                    if value and name and value != name:
                        text = f"{name}={value}"
                    is_container = role in _CONTAINER_ROLES
                    if not is_container or text:
                        states = _states_of(child)
                        if "offscreen" not in states:
                            elements.append(
                                UIElement(
                                    source="uia",
                                    role=role,
                                    text=text,
                                    cx=(rect.left + rect.right) // 2,
                                    cy=(rect.top + rect.bottom) // 2,
                                    left=rect.left,
                                    top=rect.top,
                                    width=width,
                                    height=height,
                                    states=states,
                                    extra={
                                        "automation_id": child.AutomationId or "",
                                        "class_name": child.ClassName or "",
                                        "control_type": ctype,
                                        "uia_ctrl": child,
                                    },
                                )
                            )
                walk(child, depth + 1)
            except Exception:
                continue

    try:
        walk(root, 0)
    except Exception as exc:
        logger.warning("UIA: 遍历异常 hwnd=%s: %s", hwnd, exc)
    return elements


def find_by_automation_id(hwnd: int, automation_id: str) -> UIElement | None:
    for element in collect(hwnd):
        if element.extra.get("automation_id") == automation_id:
            return element
    return None
