"""Installed-app discovery: resolve human app names (incl. Chinese display
names) to launchable targets.

Why: ``find_app`` only sees RUNNING processes and ``launch`` only takes exe
names/paths — asked to open 网易云音乐 the agent had no way to learn that the
binary is ``cloudmusic.exe`` and got lost searching the disk. Display names
live in Start-Menu shortcuts and the UWP app list; this module indexes those.

Sources (merged, deduped, scored):
- Start Menu ``*.lnk`` (user + common) — shortcut filename IS the display name
- ``Get-StartApps`` (covers UWP/Store apps and most desktop apps)
- App Paths registry (classic exe aliases)
- Everything filename search as a last resort (*.exe)
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from ..utils.log import logger

_CACHE_TTL_S = 300.0
_cache_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]
_SKIP_WORDS = ("卸载", "uninstall", "帮助", "help", "网站", "website", "readme")


def _start_menu_entries() -> list[dict]:
    entries: list[dict] = []
    for base in _START_MENU_DIRS:
        if not base.exists():
            continue
        try:
            for lnk in base.rglob("*.lnk"):
                name = lnk.stem
                if any(w in name.lower() for w in _SKIP_WORDS):
                    continue
                entries.append({"name": name, "target": str(lnk), "kind": "lnk"})
        except OSError as exc:
            logger.debug("开始菜单扫描失败 %s: %s", base, exc)
    return entries


def _uwp_entries() -> list[dict]:
    """Get-StartApps: display name -> AppID (covers UWP + many desktop apps).

    Store apps (e.g. 网易云音乐) have NO classic .lnk/.exe — this list is the
    only way to find them. Console output must be forced to UTF-8: PowerShell
    5.1 pipes in the ANSI codepage (GBK here) which would mangle Chinese names.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        entries = []
        for line in (out.stdout or "").splitlines():
            if "|" not in line:
                continue
            name, appid = line.split("|", 1)
            name, appid = name.strip(), appid.strip()
            if name and appid:
                entries.append({"name": name, "target": appid, "kind": "appid"})
        return entries
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Get-StartApps 失败: %s", exc)
        return []


def _app_paths_entries() -> list[dict]:
    import winreg

    entries: list[dict] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                root, r"Software\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub) as sk:
                        path, _ = winreg.QueryValueEx(sk, None)
                    if path:
                        entries.append({"name": Path(sub).stem,
                                        "target": str(path).strip('"'),
                                        "kind": "exe"})
                except OSError:
                    continue
        finally:
            key.Close()
    return entries


def _index() -> list[dict]:
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_S:
            return _cache[1]
    entries = _start_menu_entries() + _uwp_entries() + _app_paths_entries()
    # Dedupe by (lowered name, kind) keeping first occurrence.
    seen: set[tuple[str, str]] = set()
    deduped = []
    for e in entries:
        key = (e["name"].lower(), e["kind"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    with _cache_lock:
        _cache = (time.monotonic(), deduped)
    logger.info("应用索引就绪: %d 条 (开始菜单+UWP+注册表)", len(deduped))
    return deduped


def _score(name_lower: str, query_lower: str) -> int:
    if name_lower == query_lower:
        return 100
    if name_lower.startswith(query_lower):
        return 80
    if query_lower in name_lower:
        return 60
    return 0


def resolve(query: str, max_results: int = 5) -> list[dict]:
    """Installed-app candidates for a human name, best match first."""
    q = (query or "").strip().lower()
    if not q:
        return []
    scored = []
    for e in _index():
        s = _score(e["name"].lower(), q)
        if s:
            # shortcuts are the most reliable launch targets; appid next
            s += {"lnk": 2, "appid": 1}.get(e["kind"], 0)
            scored.append((s, e))
    scored.sort(key=lambda t: -t[0])
    results = [e for _s, e in scored[:max_results]]
    if results:
        return results
    # Last resort: Everything filename search for an exe.
    try:
        from . import search
        hits = search.search(f"{query}*.exe", max_results=max_results)
        return [{"name": Path(p).stem, "target": p, "kind": "exe"}
                for p in (hits.get("results") or [])
                if not any(w in p.lower() for w in _SKIP_WORDS)]
    except Exception as exc:
        logger.debug("Everything 应用兜底失败: %s", exc)
        return []


def launch_target(entry: dict) -> dict:
    """Launch a resolved candidate. Returns {ok, via, target}."""
    kind, target = entry.get("kind"), entry.get("target", "")
    try:
        if kind == "appid":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
        else:  # lnk / exe — startfile resolves shortcuts natively
            os.startfile(target)  # noqa: S606
        return {"ok": True, "via": kind, "target": target, "name": entry.get("name")}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "target": target}
