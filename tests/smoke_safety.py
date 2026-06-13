"""Smoke test for the safety layer: catastrophic always blocked; dangerous
gated by approve_mode; normal commands allowed."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.action import safety  # noqa: E402
from winpilot.config import CONFIG  # noqa: E402


def _mode(m: str) -> None:
    CONFIG._data["agent"]["approve_mode"] = m  # noqa: SLF001 (test only)


def test_catastrophic_always_blocked() -> None:
    _mode("off")  # even with approvals off, catastrophic is blocked
    for cmd in [
        "format C: /q",
        "Format-Volume C: -Confirm:0",
        "Stop-Computer -Force",
        "shutdown /r /t 0",
        "shutdown /g",
        "Remove-Item -Recurse -Force C:\\Windows",
        "Remove-Item -Recurse $env:USERPROFILE",
        "vssadmin delete shadows /all",
        "bcdedit /set nx AlwaysOff",
        "diskpart",
        "powershell -enc RgBvAHIAbQBhAHQAIABDADoA",
        "powershell -EncodedCommand SGVsbG8gd29ybGQgYmFzZTY0eHh4",
    ]:
        action, _ = safety.evaluate(cmd)
        assert action == "block", f"应拦截灾难性命令: {cmd}"
    print("[OK] 灾难性命令永远拦截(含 EncodedCommand 绕过)")


def test_no_false_positive_on_single_file_delete() -> None:
    _mode("off")
    # deleting a single system file is NOT catastrophic (no recurse flag)
    assert safety.evaluate("Remove-Item C:\\Windows\\Temp\\one.txt")[0] == "allow"
    assert safety.evaluate("iwr https://x -OutFile a.txt")[0] == "allow"  # normal download
    print("[OK] 单文件删/普通下载不误杀")


def test_normal_allowed() -> None:
    _mode("off")
    for cmd in [
        "Get-Process",
        "New-Item -ItemType Directory -Path C:\\Users\\me\\Desktop\\x",
        "Test-Path C:\\Windows",
        "Start-Process calc",
        'Remove-Item C:\\Users\\me\\Desktop\\one_file.txt',  # single file, no -Recurse
        "git clone https://github.com/x/y",
    ]:
        action, _ = safety.evaluate(cmd)
        assert action == "allow", f"正常命令不应拦: {cmd}"
    print("[OK] 正常命令放行")


def test_dangerous_gated_by_mode() -> None:
    danger = "Remove-Item -Recurse -Force C:\\Users\\me\\Desktop\\proj"
    _mode("off")
    assert safety.evaluate(danger)[0] == "allow", "off 模式危险操作放行"
    _mode("guard")
    assert safety.evaluate(danger)[0] == "block", "guard 模式应拦危险操作"
    _mode("dry_run")
    assert safety.evaluate(danger)[0] == "dry_run", "dry_run 模式应模拟"
    _mode("off")
    print("[OK] 危险操作按 approve_mode 分级(off放行/guard拦/dry_run模拟)")


def test_guard_dict() -> None:
    _mode("off")
    assert safety.guard("Get-Process") is None
    blocked = safety.guard("format D: /q")
    assert blocked and blocked["ok"] is False and blocked.get("blocked")
    _mode("dry_run")
    dr = safety.guard("Remove-Item -Recurse C:\\Users\\me\\Desktop\\p")
    assert dr and dr.get("dry_run") and dr["ok"]
    _mode("off")
    print("[OK] guard() 返回拦截/模拟结果")


def test_shell_integration() -> None:
    from winpilot.action import shell
    _mode("off")
    r = shell.run("format C: /q")  # catastrophic → blocked, never executes
    assert not r.get("ok") and r.get("blocked"), r
    print("[OK] shell.run 集成安全层(灾难命令被拦,未执行)")


if __name__ == "__main__":
    test_catastrophic_always_blocked()
    test_no_false_positive_on_single_file_delete()
    test_normal_allowed()
    test_dangerous_gated_by_mode()
    test_guard_dict()
    test_shell_integration()
    print("\n安全层 smoke 全部通过 ✓")
