"""Bilingual app-name aliases: bridge the Chinese name the agent says (and
expects, e.g. window_appears "计算器") to the actual window title Windows shows
in English ("Calculator").

Without this, verify on English-titled built-in apps (Calculator, Settings,
many UWP apps) always fails → the run never counts as verified → no reusable
script is exported → skill reuse can't kick in. The map is small and additive;
unknown names just return themselves (no behavior change).
"""
from __future__ import annotations

# Each group lists interchangeable names/title-substrings for one app. Matching
# is case-insensitive substring, so short stable fragments are best.
_ALIAS_GROUPS: list[tuple[str, ...]] = [
    ("计算器", "calculator"),
    ("设置", "settings"),
    ("记事本", "notepad"),
    ("画图", "paint", "mspaint"),
    ("文件资源管理器", "资源管理器", "file explorer", "explorer"),
    ("任务管理器", "task manager"),
    ("命令提示符", "command prompt", "cmd"),
    ("终端", "terminal", "windows terminal"),
    ("时钟", "clock", "闹钟"),
    ("照片", "photos"),
    ("相机", "camera"),
    ("日历", "calendar"),
    ("天气", "weather"),
    ("地图", "maps"),
    ("商店", "microsoft store", "store"),
    ("便笺", "sticky notes"),
    ("录音机", "voice recorder", "sound recorder"),
    ("控制面板", "control panel"),
    ("注册表编辑器", "registry editor", "regedit"),
    ("画图3d", "paint 3d"),
    ("媒体播放器", "media player"),
]

# name(lower) -> set of all equivalents(lower)
_INDEX: dict[str, set[str]] = {}
for _group in _ALIAS_GROUPS:
    _lowered = {g.lower() for g in _group}
    for _name in _lowered:
        _INDEX.setdefault(_name, set()).update(_lowered)


def expand(name: str) -> list[str]:
    """Return name plus all known equivalents (lowercased, deduped).

    Substring-aware: if the query contains a known alias (e.g. "打开计算器"),
    that alias's group is included too.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return []
    extras: set[str] = set()
    for known, group in _INDEX.items():
        if known in raw or raw in known:
            extras.update(group)
    extras.discard(raw)
    return [raw] + sorted(extras)  # literal query first, then aliases
