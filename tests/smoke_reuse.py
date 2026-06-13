"""Deterministic test of the skill-reuse PATH (no LLM, no model disposition).

Seeds a recipe + a real .wps.json replay script (shell steps that create
files), then invokes the reuse_skill tool directly and asserts it 0-token
replays the procedure (files appear) and reports reuse. This isolates the
reuse mechanism from whether the model *chooses* to call it on a given task.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.config import SCRIPTS_DIR  # noqa: E402
from winpilot.memory import experience  # noqa: E402

_DIR = Path(tempfile.mkdtemp()) / "winpilot_reuse_unit"


def _cleanup() -> None:
    import shutil
    shutil.rmtree(_DIR, ignore_errors=True)


def test_reuse_skill_replays_script() -> None:
    experience.MEMORY_PATH = Path(tempfile.mkdtemp()) / "m.json"
    _cleanup()
    # Build a real replay script: two run_shell steps that create files.
    script = {
        "format": "wps/1", "name": "reuse_unit", "task": "复用单元测试任务",
        "steps": [
            {"tool": "run_shell",
             "command": f'New-Item -ItemType Directory -Force -Path "{_DIR}" | Out-Null; '
                        f'Set-Content -Path "{_DIR}\\a.txt" -Value REUSEA'},
            {"tool": "run_shell",
             "command": f'Set-Content -Path "{_DIR}\\b.txt" -Value REUSEB'},
        ],
    }
    SCRIPTS_DIR.mkdir(exist_ok=True)
    sp = SCRIPTS_DIR / "reuse_unit.wps.json"
    sp.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    experience.record("复用单元测试任务", "shell", ["run_shell ...", "run_shell ..."],
                      verified=True, script="reuse_unit.wps.json")

    from winpilot.agent.tools import ToolSession
    s = ToolSession()
    try:
        r = s.dispatch("reuse_skill", {"task": "复用单元测试任务"})
        assert "复用" in r.text and "0-token" in r.text, r.text
        a = (_DIR / "a.txt")
        b = (_DIR / "b.txt")
        assert a.exists() and "REUSEA" in a.read_text(encoding="utf-8", errors="replace")
        assert b.exists() and "REUSEB" in b.read_text(encoding="utf-8", errors="replace")
        print(f"[OK] reuse_skill 0-token 重放脚本: 两个文件由复用创建 | {r.text[:50]}")
    finally:
        _cleanup()
        sp.unlink(missing_ok=True)


def test_reuse_skill_no_script() -> None:
    experience.MEMORY_PATH = Path(tempfile.mkdtemp()) / "m2.json"
    from winpilot.agent.tools import ToolSession
    s = ToolSession()
    r = s.dispatch("reuse_skill", {"task": "根本没有的任务xyz"})
    assert "没有可复用" in r.text or "实时操作" in r.text
    print("[OK] 无匹配脚本时 reuse_skill 提示改用实时操作")


def test_reusable_script_for_resolution() -> None:
    experience.MEMORY_PATH = Path(tempfile.mkdtemp()) / "m3.json"
    experience.record("打开计算器算888", "计算器", ["s"], verified=True,
                      script="calc888.wps.json")
    script, recipe = experience.reusable_script_for("打开计算器算888")
    assert script == "calc888.wps.json" and recipe is not None
    s2, _ = experience.reusable_script_for("毫不相关xyz")
    assert s2 == ""
    print("[OK] reusable_script_for 强匹配解析")


if __name__ == "__main__":
    subprocess.run(["powershell", "-Command", "$null"], capture_output=True)  # warm
    test_reuse_skill_replays_script()
    test_reuse_skill_no_script()
    test_reusable_script_for_resolution()
    print("\n复用机制 smoke 全部通过 ✓")
