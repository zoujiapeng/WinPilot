"""Eval task suite: real tasks with PROGRAMMATIC ground-truth checks.

A task's success is decided by ``check(ctx)`` inspecting the real world
(filesystem / processes / the agent's final answer text) — never by the
agent's own "done" claim. This is the external standard that makes "did a
change help or regress?" answerable.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..perception import groundtruth as gt

DESKTOP = Path.home() / "Desktop"


def _noop() -> None:
    pass


def _rm(path: Path) -> None:
    try:
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _kill(image: str) -> None:
    subprocess.run(["taskkill", "/f", "/im", image], capture_output=True)


@dataclass
class EvalTask:
    id: str
    prompt: str
    check: Callable[[dict], tuple[bool, str]]
    setup: Callable[[], None] = _noop
    cleanup: Callable[[], None] = _noop
    tags: tuple[str, ...] = ()
    max_steps: int = 40
    repeat: int = 1  # run N times; ctx['run_ids'] holds all (for reuse tests)


# ----------------------------------------------------------------- tasks
_DIR = DESKTOP / "winpilot_eval_dir"
_TXT = DESKTOP / "winpilot_eval.txt"
_NOTE = DESKTOP / "winpilot_eval_note.txt"
_REPO = DESKTOP / "Hello-World"


def _check_dir(_ctx: dict) -> tuple[bool, str]:
    return gt.file_exists(str(_DIR))


def _check_txt(_ctx: dict) -> tuple[bool, str]:
    return gt.file_contains(str(_TXT), "EVALOK123")


def _check_note(_ctx: dict) -> tuple[bool, str]:
    return gt.file_contains(str(_NOTE), "EVALNOTE")


def _check_calc(_ctx: dict) -> tuple[bool, str]:
    return gt.process_running("CalculatorApp")


def _check_timer(_ctx: dict) -> tuple[bool, str]:
    # the scheduled launch fires after the agent finishes; poll a bit
    for _ in range(14):
        ok, detail = gt.process_running("CalculatorApp")
        if ok:
            return True, detail
        time.sleep(2)
    return False, "等待后计算器仍未出现"


def _check_clone(_ctx: dict) -> tuple[bool, str]:
    ok1, _ = gt.file_exists(str(_REPO / ".git"))
    ok2, _ = gt.file_exists(str(_REPO / "README"))
    return (ok1 and ok2), f".git={ok1} README={ok2}"


def _check_compute(ctx: dict) -> tuple[bool, str]:
    text = ctx.get("result_text", "")
    return ("408" in text), f'回复{"含" if "408" in text else "不含"}408: {text[:60]!r}'


SUITE: list[EvalTask] = [
    EvalTask(
        id="shell_file", tags=("cheap",), max_steps=15,
        prompt="在我的桌面上新建一个空文件夹，名字叫 winpilot_eval_dir",
        setup=lambda: _rm(_DIR), cleanup=lambda: _rm(_DIR), check=_check_dir),
    EvalTask(
        id="file_write", tags=("cheap",), max_steps=15,
        prompt="在我的桌面创建一个文本文件 winpilot_eval.txt，内容写入 EVALOK123",
        setup=lambda: _rm(_TXT), cleanup=lambda: _rm(_TXT), check=_check_txt),
    EvalTask(
        id="launch_calc", tags=("cheap",), max_steps=15,
        prompt="打开计算器",
        setup=lambda: _kill("CalculatorApp.exe"),
        cleanup=lambda: _kill("CalculatorApp.exe"), check=_check_calc),
    EvalTask(
        id="timer", tags=("cheap",), max_steps=15,
        prompt="过8秒后打开计算器",
        setup=lambda: _kill("CalculatorApp.exe"),
        cleanup=lambda: _kill("CalculatorApp.exe"), check=_check_timer),
    EvalTask(
        id="git_clone", tags=("medium",), max_steps=30,
        prompt="克隆 https://github.com/octocat/Hello-World 这个仓库到我的桌面",
        setup=lambda: _rm(_REPO), cleanup=lambda: _rm(_REPO), check=_check_clone),
    EvalTask(
        id="calc_compute", tags=("gui",), max_steps=40,
        prompt="打开计算器，计算 12 乘以 34，把结果数字告诉我",
        setup=lambda: _kill("CalculatorApp.exe"),
        cleanup=lambda: _kill("CalculatorApp.exe"), check=_check_compute),
    EvalTask(
        id="notepad_write", tags=("gui",), max_steps=40,
        prompt="打开记事本，输入文字 EVALNOTE，另存为到桌面 winpilot_eval_note.txt",
        setup=lambda: (_rm(_NOTE), _kill("notepad.exe")),
        cleanup=lambda: (_rm(_NOTE), _kill("notepad.exe")), check=_check_note),
]
# NOTE: the skill-reuse path is validated by a deterministic unit test
# (tests/smoke_reuse.py) rather than an eval task — whether the model *chooses*
# to call reuse_skill on an easy task is model-disposition, not task success,
# and doubly flaky to score. The eval suite measures real task success only.

_SMOKE_IDS = {"shell_file", "launch_calc", "timer"}


def suite(name: str = "full") -> list[EvalTask]:
    if name == "smoke":
        return [t for t in SUITE if t.id in _SMOKE_IDS]
    return list(SUITE)


def by_ids(ids: list[str]) -> list[EvalTask]:
    want = set(ids)
    return [t for t in SUITE if t.id in want]
