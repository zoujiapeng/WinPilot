"""Shell execution: run cmd/PowerShell commands and capture their output.

The agent's ``launch`` tool is fire-and-forget (Popen) and returns no output —
useless for "list files / read result / query system" work. ``run`` here uses
subprocess.run with full stdout/stderr/exit-code capture, a hard timeout, and
UTF-8 decoding so the agent can actually see what a command produced.

Gated by ``config.agent.shell_enabled``: turning it off forces pure-GUI mode
(for validating that GUI-only paths work without any shell shortcuts).
"""
from __future__ import annotations

import subprocess

from ..config import CONFIG
from ..utils.log import BUS, logger

_DEFAULT_TIMEOUT_S = 30
_MAX_OUTPUT_CHARS = 6000  # what we hand back to the LLM (full text still logged)


def shell_enabled() -> bool:
    return bool(CONFIG.get("agent", "shell_enabled", default=True))


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n...（输出已截断 {len(text) - limit} 字符）...\n{tail}"


def run(command: str, shell: str = "powershell",
        timeout_s: float = _DEFAULT_TIMEOUT_S, cwd: str | None = None) -> dict:
    """Run a command, capture output. Never raises — errors come back in the dict.

    shell: "powershell" (default) or "cmd".
    """
    if not shell_enabled():
        return {"ok": False, "error": "shell 已被禁用（强制 GUI 模式）。请改用界面操作工具。"}
    if not command or not command.strip():
        return {"ok": False, "error": "命令为空"}

    if shell == "cmd":
        argv = ["cmd", "/c", command]
    else:
        # -NoProfile keeps startup fast and deterministic; bypass execution policy
        # so ad-hoc scripts run without per-machine policy friction.
        argv = ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]

    BUS.publish("action", action="shell", shell=shell, command=command[:400])
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=cwd,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.warning("shell 命令超时(%ss): %s", timeout_s, command[:120])
        return {"ok": False, "error": f"命令超时（{timeout_s}s）", "timeout": True}
    except Exception as exc:
        logger.warning("shell 执行异常: %s", exc)
        return {"ok": False, "error": str(exc)}

    stdout = _truncate(proc.stdout)
    stderr = _truncate(proc.stderr)
    ok = proc.returncode == 0
    BUS.publish("action", action="shell_result", exit_code=proc.returncode,
                ok=ok, output_preview=(stdout or stderr)[:300])
    return {
        "ok": ok,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
