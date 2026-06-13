"""Deterministic ground-truth checks — the Codex-grade verification primitive.

GUI success is hard to verify from pixels, but the *deterministic substrate*
a task touches (filesystem, processes, shell exit codes) can be checked
exactly. These helpers are the shared truth source used both by the runtime
verify engine (action/verify.py expect DSL) and by the eval harness checkers.

All helpers return (ok: bool, detail: str) and never raise.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..utils.log import logger


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def file_exists(path: str) -> tuple[bool, str]:
    try:
        p = _expand(path)
        exists = p.exists()
        return exists, f'{"存在" if exists else "不存在"}: {p}'
    except OSError as exc:
        return False, f"路径检查失败: {exc}"


def file_contains(path: str, text: str, encoding: str = "utf-8") -> tuple[bool, str]:
    try:
        p = _expand(path)
        if not p.exists():
            return False, f"文件不存在: {p}"
        # tolerate BOM / alternate encodings, fall back to bytes search
        try:
            content = p.read_text(encoding=encoding, errors="replace")
        except OSError as exc:
            return False, f"读取失败: {exc}"
        ok = str(text) in content
        return ok, (f'文件含 "{text}"' if ok else f'文件不含 "{text}"（前80字: {content[:80]!r}）')
    except OSError as exc:
        return False, f"检查失败: {exc}"


def _process_names() -> set[str]:
    """Lowercased set of running process image names (best-effort)."""
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        names = set()
        for line in (out.stdout or "").splitlines():
            if line.startswith('"'):
                names.add(line.split('","', 1)[0].strip('"').lower())
        return names
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("tasklist 失败: %s", exc)
        return set()


def process_running(name: str) -> tuple[bool, str]:
    needle = str(name).lower().removesuffix(".exe")
    procs = _process_names()
    # Prefer exact / exe-name match; fall back to substring (short needles can
    # over-match, so exact wins).
    hit = next((p for p in procs if p == needle or p == needle + ".exe"
                or p.removesuffix(".exe") == needle), None)
    if hit is None:
        hit = next((p for p in procs if needle in p), None)
    return (hit is not None), (f"进程运行中: {hit}" if hit else f'无匹配 "{name}" 的进程')


def process_gone(name: str) -> tuple[bool, str]:
    ok, detail = process_running(name)
    return (not ok), (f'进程仍在: {name}' if ok else f'进程已退出: {name}')


def shell_true(expr: str, timeout_s: float = 15.0) -> tuple[bool, str]:
    """Run a PowerShell expression; success = exit 0 AND output is truthy.

    Truthy = non-empty stdout that isn't 'false'/'0'. Lets a task assert any
    deterministic condition, e.g. "(Test-Path X) -and (Get-Process Y)".
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + str(expr)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, f"shell_true 超时({timeout_s}s)"
    except OSError as exc:
        return False, f"shell_true 执行失败: {exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}: {(proc.stderr or out)[:120]}"
    ok = bool(out) and out.lower() not in ("false", "0")
    return ok, f"输出: {out[:120]!r}"


# Dispatch by expect-DSL key → used by verify.py
def check(key: str, value, timeout_s: float = 15.0) -> tuple[bool, str]:
    if key == "file_exists":
        return file_exists(str(value))
    if key == "file_contains":
        if not isinstance(value, dict) or "path" not in value or "text" not in value:
            return False, "file_contains 需 {path, text}"
        return file_contains(str(value["path"]), str(value["text"]),
                             encoding=str(value.get("encoding", "utf-8")))
    if key == "process_running":
        return process_running(str(value))
    if key == "process_gone":
        return process_gone(str(value))
    if key == "shell_true":
        return shell_true(str(value), timeout_s=max(1.0, timeout_s))
    return False, f"未知 ground-truth 条件: {key}"


GROUNDTRUTH_KEYS = (
    "file_exists", "file_contains", "process_running", "process_gone", "shell_true",
)
