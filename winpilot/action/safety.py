"""Safety layer: classify shell commands by destructiveness and gate them.

Two tiers:
- CATASTROPHIC — irreversible machine-level harm (format, shutdown/restart,
  recursive delete of system/user-root dirs, boot config, shadow-copy delete).
  ALWAYS blocked regardless of mode — protects the machine even during
  unattended autonomous runs.
- DANGEROUS — destructive but plausibly intended (recursive/force delete of a
  normal path, reg delete, bulk taskkill). Gated by ``agent.approve_mode``.

``approve_mode``: "off" (default; dangerous allowed, catastrophic still blocked)
| "guard" (dangerous blocked with explanation) | "dry_run" (dangerous simulated).
"""
from __future__ import annotations

import re

from ..config import CONFIG
from ..utils.log import logger

# Always-block: irreversible, machine-level. These run for cmd or powershell.
_CATASTROPHIC = [
    # Obfuscation vectors — refuse outright (a hidden command can't be vetted).
    (re.compile(r"-e(nc(odedcommand)?|c)?\b\s+[A-Za-z0-9+/=]{16,}", re.I),
     "PowerShell EncodedCommand(无法审查的编码命令,一律拒绝)"),
    (re.compile(r"\bformat\b\s+[a-z]:", re.I), "格式化磁盘"),
    (re.compile(r"\bFormat-Volume\b", re.I), "格式化卷(Format-Volume)"),
    (re.compile(r"\bdiskpart\b", re.I), "diskpart 磁盘操作"),
    (re.compile(r"\b(Stop|Restart)-Computer\b", re.I), "关机/重启"),
    (re.compile(r"\bshutdown\b\s+/[rsgp]", re.I), "shutdown 关机/重启"),
    (re.compile(r"\bbcdedit\b", re.I), "引导配置修改"),
    (re.compile(r"\bvssadmin\b.*\bdelete\b", re.I), "删除卷影副本"),
    (re.compile(r"\bcipher\b\s+/w", re.I), "擦除磁盘空间"),
    (re.compile(r"\bwmic\b.*\bos\b.*\b(shutdown|reboot)\b", re.I), "WMIC 关机"),
    # recursive/force delete of a system or user-root directory — the destructive
    # FLAG is required (single-file delete in a system path is not catastrophic).
    (re.compile(
        r"(Remove-Item|rm|del|rd|rmdir)\b[^|;\n]*"
        r"(-Recurse|-Force|-r\b|-rf\b|/s\b)[^|;\n]*"
        r"(C:\\?\s*$|C:\\Windows|System32|%SystemRoot%|\$env:WINDIR|"
        r"%USERPROFILE%\\?\s*$|\$env:USERPROFILE\\?\s*$|C:\\Users\\?\s*$)",
        re.I), "递归删除系统/用户根目录"),
    (re.compile(r"\brm\b\s+-rf\s+(/|~|\$HOME)\s*$", re.I), "rm -rf 根/家目录"),
]

# Block in guard/dry_run: destructive but possibly intended.
_DANGEROUS = [
    (re.compile(r"(Remove-Item|rm)\b[^|;\n]*(-Recurse|-Force|-r\b|-rf\b)", re.I),
     "递归/强制删除"),
    (re.compile(r"\b(del|erase)\b[^|;\n]*/[fsq]", re.I), "del /f/s/q 批量删除"),
    (re.compile(r"\b(rd|rmdir)\b\s+/s", re.I), "rd /s 删目录树"),
    (re.compile(r"\breg\b\s+delete\b", re.I), "删除注册表项"),
    (re.compile(r"Remove-Item\b.*HK(LM|CU|CR|U|CC):", re.I), "删除注册表项"),
    (re.compile(r"\btaskkill\b.*/f.*\*", re.I), "批量强杀进程"),
    (re.compile(r"Stop-Process\b.*-Force.*-Name\s+\*", re.I), "批量强杀进程"),
    (re.compile(r"Set-ExecutionPolicy\b.*Unrestricted", re.I), "放开执行策略"),
    (re.compile(r"\b(iex|Invoke-Expression)\b.*(Invoke-WebRequest|iwr|DownloadString|curl|wget)", re.I),
     "下载并执行远程代码"),
    (re.compile(r"(Invoke-WebRequest|iwr|DownloadString).*\|\s*(iex|Invoke-Expression)\b", re.I),
     "下载并执行远程代码"),
]


def approve_mode() -> str:
    mode = str(CONFIG.get("agent", "approve_mode", default="off")).lower()
    return mode if mode in ("off", "guard", "dry_run") else "off"


def evaluate(command: str) -> tuple[str, str]:
    """Classify a command. Returns (action, reason):
        action ∈ {"allow", "block", "dry_run"}.
    Catastrophic → always "block". Dangerous → per approve_mode.
    """
    cmd = command or ""
    for pat, why in _CATASTROPHIC:
        if pat.search(cmd):
            return "block", f"灾难性操作已拦截（{why}）：此类不可逆操作被永久禁止。"
    mode = approve_mode()
    if mode == "off":
        return "allow", ""
    for pat, why in _DANGEROUS:
        if pat.search(cmd):
            if mode == "dry_run":
                return "dry_run", f"危险操作（{why}）在 dry_run 模式下未真正执行。"
            return "block", (f"危险操作（{why}）被审批模式 guard 拦截。"
                             "若确属任务需要，请改用更精确受限的命令或说明。")
    return "allow", ""


def guard(command: str) -> dict | None:
    """Return a blocking/dry-run result dict if the command should not run,
    else None (allowed). Logs blocks."""
    action, reason = evaluate(command)
    if action == "allow":
        return None
    logger.warning("安全层 %s: %s | cmd=%s", action, reason, command[:120])
    if action == "dry_run":
        return {"ok": True, "dry_run": True, "blocked": False, "note": reason,
                "stdout": "", "stderr": "", "exit_code": 0}
    return {"ok": False, "blocked": True, "error": reason}
