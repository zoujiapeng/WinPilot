"""Smoke test for run_detached (durable background/timer primitive)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.action import shell  # noqa: E402


def _calc_running() -> bool:
    out = subprocess.run(["tasklist"], capture_output=True, text=True,
                         errors="replace")
    return "CalculatorApp.exe" in out.stdout


def test_detached_survives_and_fires() -> None:
    # close any stray calc first
    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"],
                   capture_output=True)
    assert not _calc_running(), "测试前应无计算器"

    r = shell.run_detached("Start-Sleep 5; Start-Process calc", name="smoke定时")
    assert r["ok"] and r["pid"], f"启动失败: {r}"
    assert Path(r["script_path"]).exists(), "脚本文件应已写入"
    print(f'[OK] 分离进程已启动 PID={r["pid"]} 脚本={Path(r["script_path"]).name}')

    # immediately: calc must NOT be up yet (proves the delay is real)
    assert not _calc_running(), "5秒延时未到，计算器不应已出现"
    # PID must be alive (proves it didn't die with the call)
    alive = subprocess.run(["tasklist", "/fi", f"PID eq {r['pid']}"],
                           capture_output=True, text=True, errors="replace")
    assert str(r["pid"]) in alive.stdout, "分离进程应仍存活"
    print("[OK] 命令返回后进程仍存活、延时未到计算器未开")

    # wait past the delay -> calc should appear
    fired = False
    for _ in range(8):
        time.sleep(2)
        if _calc_running():
            fired = True
            break
    assert fired, "延时到点后计算器应自动打开"
    print("[OK] 到点后计算器真实自动打开")

    subprocess.run(["taskkill", "/f", "/im", "CalculatorApp.exe"], capture_output=True)
    Path(r["script_path"]).unlink(missing_ok=True)


def test_disabled_guard(monkeypatch=None) -> None:
    # when shell disabled, must refuse
    from winpilot.config import CONFIG
    orig = CONFIG.get("agent", "shell_enabled", default=True)
    CONFIG._data["agent"]["shell_enabled"] = False
    try:
        r = shell.run_detached("Start-Process calc", name="x")
        assert not r["ok"] and "禁用" in r["error"]
        print("[OK] shell 禁用时拒绝 run_detached")
    finally:
        CONFIG._data["agent"]["shell_enabled"] = orig


if __name__ == "__main__":
    test_detached_survives_and_fires()
    test_disabled_guard()
    print("\nrun_detached smoke 全部通过 ✓")
