"""Smoke test for bilingual app-name aliases in window finding."""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.perception import win32  # noqa: E402
from winpilot.perception.app_aliases import expand  # noqa: E402


def test_expand() -> None:
    assert expand("计算器")[0] == "计算器", "原始查询应在首位"
    assert "calculator" in expand("计算器")
    assert "计算器" in expand("Calculator")
    assert "settings" in expand("设置")
    # substring query
    assert "calculator" in expand("打开计算器"), "含别名的子串查询也应展开"
    # unknown name → just itself
    assert expand("某不存在应用XYZ") == ["某不存在应用xyz"]
    assert expand("") == []
    print("[OK] expand 双向别名 + 子串 + 未知名")


def test_find_window_alias_live() -> None:
    """Open Calculator (English title) and find it by the Chinese name."""
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)
    subprocess.Popen(["calc"])
    # calc may relaunch via CalculatorApp; wait for the window
    found = None
    for _ in range(15):
        time.sleep(1)
        found = win32.find_window("计算器")
        if found:
            break
    try:
        assert found, "用中文'计算器'应能找到英文标题 Calculator 窗口"
        print(f'[OK] find_window("计算器") → "{found["title"]}"')
        # literal English still works
        assert win32.find_window("Calculator"), "英文字面也应能找到"
        print("[OK] 中英双向都能定位计算器窗口")
    finally:
        subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)


if __name__ == "__main__":
    test_expand()
    test_find_window_alias_live()
    print("\n应用别名 smoke 全部通过 ✓")
