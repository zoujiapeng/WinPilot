"""Smoke test: find_app + tray icon enumeration (visible action: opens overflow)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winpilot.perception import tray, win32  # noqa: E402

info = win32.find_app("QQ")
print("find_app(QQ):", {k: v for k, v in info.items() if k != "windows"})
print("  windows:", info["windows"][:5])
assert info["running"], "QQ 应在运行"

icons = tray.list_tray_icons(expand_overflow=True)
print(f"托盘图标 {len(icons)} 个:")
for icon in icons:
    print(f"  [{icon.extra.get('area')}] {icon.text!r} ({icon.cx},{icon.cy})")

qq_hits = [i for i in icons if "qq" in i.text.lower()]
print("QQ 图标:", [(i.text, i.extra.get("area")) for i in qq_hits])
assert qq_hits, "托盘中应能找到 QQ 图标"

# close the overflow flyout if we opened it
from winpilot.action import executor
executor.press_keys(["escape"])
print("SMOKE TRAY OK")
