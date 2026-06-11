"""Fast file search: Everything (es.exe) with a PowerShell fallback.

Everything indexes the NTFS MFT, so filename search across the whole disk is
near-instant — vastly faster than walking directories with ``dir /s``. We
shell out to its CLI ``es.exe``. If es.exe is missing or can't reach the
running Everything instance (e.g. Everything runs elevated and we don't —
UIPI hides its IPC window), we fall back to a scoped PowerShell walk.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..config import BIN_DIR, CONFIG
from ..utils.log import BUS, logger

_DEFAULT_MAX = 50
_everything_client_tried = False  # only auto-launch the user-session client once


def find_es() -> str | None:
    """Locate es.exe: config override → project bin/ → PATH → Everything dir."""
    override = CONFIG.get("search", "everything_es_path", default="")
    candidates = [override] if override else []
    candidates.append(str(BIN_DIR / "es.exe"))
    candidates.extend([
        r"C:\Program Files\Everything\es.exe",
        r"C:\Program Files (x86)\Everything\es.exe",
    ])
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    # bare name on PATH
    import shutil
    return shutil.which("es") or shutil.which("es.exe")


def _find_everything_exe() -> str | None:
    for cand in (r"C:\Program Files\Everything\Everything.exe",
                 r"C:\Program Files (x86)\Everything\Everything.exe"):
        if Path(cand).exists():
            return cand
    return None


def _ensure_everything_client() -> bool:
    """When Everything runs as a Session-0 service, es.exe (in the user session)
    can't find its IPC window. Launching a normal user-session client connects
    to the service and exposes the IPC window. Done at most once per process."""
    global _everything_client_tried
    if _everything_client_tried:
        return False
    _everything_client_tried = True
    exe = _find_everything_exe()
    if not exe:
        return False
    try:
        # -startup launches minimized to tray without stealing focus
        subprocess.Popen([exe, "-startup"], creationflags=subprocess.CREATE_NO_WINDOW)
        logger.info("已尝试启动 Everything 用户会话客户端以提供 IPC")
        time.sleep(3.5)  # let the client register its IPC window
        return True
    except Exception as exc:
        logger.warning("启动 Everything 客户端失败: %s", exc)
        return False


def _run_es(es: str, query: str, max_results: int, path: str | None):
    argv = [es, "-n", str(max_results)]
    if path:
        argv += ["-path", path]
    argv.append(query)
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _search_everything(query: str, max_results: int, path: str | None) -> dict | None:
    """Return result dict via es.exe, or None to signal fall back."""
    es = find_es()
    if not es:
        return None
    try:
        proc = _run_es(es, query, max_results, path)
        # Error 8 = IPC window not found → try to spin up a user-session client, retry once
        if proc.returncode == 8 and _ensure_everything_client():
            proc = _run_es(es, query, max_results, path)
    except Exception as exc:
        logger.warning("es.exe 调用异常: %s", exc)
        return None
    if proc.returncode != 0:
        logger.info("es.exe 退出码 %s: %s", proc.returncode, proc.stderr.strip()[:120])
        return {
            "engine": "everything-failed",
            "es_error": proc.stderr.strip()[:200],
            "results": [],
            "count": 0,
            "hint": "es.exe 连不上 Everything（Everything 以服务模式跑在 Session 0，"
                    "需用户会话客户端提供 IPC）。已尝试自动拉起客户端；如仍失败，"
                    "手动启动一次 Everything 主程序即可。本次已自动降级。",
        }
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return {"engine": "everything", "results": lines[:max_results], "count": len(lines)}


def _search_powershell(query: str, max_results: int, path: str | None) -> dict:
    """Scoped recursive fallback. Slower; bounded by result cap + timeout."""
    root = path or str(Path.home())
    # query may be a bare name or a pattern; make it a wildcard filter
    pattern = query if any(c in query for c in "*?") else f"*{query}*"
    ps = (
        f"Get-ChildItem -LiteralPath '{root}' -Recurse -File -Filter '{pattern}' "
        f"-ErrorAction SilentlyContinue | Select-Object -First {max_results} "
        f"-ExpandProperty FullName"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return {"engine": "powershell", "results": lines[:max_results],
                "count": len(lines), "scope": root}
    except subprocess.TimeoutExpired:
        return {"engine": "powershell", "results": [], "count": 0,
                "error": f"搜索超时（已扫描 {root}），建议安装/提权 Everything 或缩小 path"}
    except Exception as exc:
        return {"engine": "powershell", "results": [], "count": 0, "error": str(exc)}


def search(query: str, max_results: int = _DEFAULT_MAX, path: str | None = None) -> dict:
    """Filename search. Everything-first, PowerShell fallback. Never raises."""
    if not query or not query.strip():
        return {"engine": "none", "results": [], "count": 0, "error": "查询为空"}
    BUS.publish("action", action="search_files", query=query, path=path or "")
    ev = _search_everything(query, max_results, path)
    if ev is not None and ev.get("engine") == "everything":
        BUS.publish("action", action="search_result", engine="everything", count=ev["count"])
        return ev
    # es missing or failed → fall back, but carry the es hint forward
    fb = _search_powershell(query, max_results, path)
    if ev is not None and ev.get("hint"):
        fb["everything_hint"] = ev["hint"]
    BUS.publish("action", action="search_result", engine=fb["engine"], count=fb["count"])
    return fb
