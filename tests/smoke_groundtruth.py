"""Smoke test for deterministic ground-truth checks + verify DSL integration."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.action import verify  # noqa: E402
from winpilot.perception import groundtruth as gt  # noqa: E402


def test_file_checks() -> None:
    d = Path(tempfile.mkdtemp())
    f = d / "gt_test.txt"
    ok, _ = gt.file_exists(str(f))
    assert not ok, "尚未创建应不存在"
    f.write_text("hello ground truth 测试", encoding="utf-8")
    ok, _ = gt.file_exists(str(f))
    assert ok, "创建后应存在"
    ok, _ = gt.file_contains(str(f), "ground truth")
    assert ok, "应含英文"
    ok, _ = gt.file_contains(str(f), "测试")
    assert ok, "应含中文"
    ok, _ = gt.file_contains(str(f), "不存在的内容")
    assert not ok
    print("[OK] file_exists / file_contains（中英文）")


def test_process_checks() -> None:
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)
    ok, _ = gt.process_running("CalculatorApp")
    assert not ok, "计算器应未运行"
    proc = subprocess.Popen(["calc"])
    import time
    found = False
    for _ in range(12):
        time.sleep(1)
        if gt.process_running("CalculatorApp")[0]:
            found = True
            break
    assert found, "启动后 process_running 应为真"
    print("[OK] process_running")
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)
    time.sleep(1)
    ok, _ = gt.process_gone("CalculatorApp")
    assert ok, "关闭后 process_gone 应为真"
    print("[OK] process_gone")


def test_shell_true() -> None:
    ok, _ = gt.shell_true("1 -eq 1")
    assert ok
    ok, _ = gt.shell_true("Test-Path C:\\Windows")
    assert ok, "C:\\Windows 应存在"
    ok, _ = gt.shell_true("Test-Path C:\\绝不存在的路径XYZ")
    assert not ok
    ok, _ = gt.shell_true("throw 'boom'")
    assert not ok, "异常/非零退出应为假"
    print("[OK] shell_true（真/假/异常）")


def test_verify_dsl_integration() -> None:
    # ground-truth keys must be recognized and evaluated by the verify engine
    assert "file_exists" in verify.SUPPORTED_CONDITIONS
    d = Path(tempfile.mkdtemp())
    f = d / "v.txt"
    f.write_text("verified-by-dsl", encoding="utf-8")
    out = verify.check({"file_exists": str(f), "file_contains": {"path": str(f), "text": "verified-by-dsl"}},
                       timeout_s=2.0)
    assert out.verified, out.detail
    out2 = verify.check({"file_exists": str(d / "nope.txt")}, timeout_s=1.0)
    assert not out2.verified and "file_exists" in out2.detail
    print("[OK] verify.check 集成 ground-truth 条件")


if __name__ == "__main__":
    test_file_checks()
    test_process_checks()
    test_shell_true()
    test_verify_dsl_integration()
    print("\n确定性验证 smoke 全部通过 ✓")
