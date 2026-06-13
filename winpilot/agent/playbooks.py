"""Playbook layer: app/situation-specific tactics as data, not prompt code.

Phase 3-H: the per-app tricks that used to be welded into prompts.py
(file-dialog method, tray wakeup, media playback) now live in
``playbooks/*.json``. Adding knowledge for a new app means dropping a JSON
file, not editing code.

Injection has two paths (the second is the anti-regression guard):
- task-keyword match at run start → into the system prompt
- runtime window-title trigger → injected mid-run by the loop the moment a
  matching dialog appears, covering tasks the keywords didn't anticipate

With ``agent.playbooks_enabled=false`` ALL playbooks are injected up front,
which is equivalent to the old monolithic prompt (safe fallback).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CONFIG, ROOT_DIR
from ..utils.log import logger

PLAYBOOKS_DIR = ROOT_DIR / "playbooks"

_HOME = Path.home()
_PATH_VARS = {
    "{desktop}": str(_HOME / "Desktop"),
    "{documents}": str(_HOME / "Documents"),
    "{downloads}": str(_HOME / "Downloads"),
}


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    content: str
    keywords: tuple[str, ...] = field(default_factory=tuple)
    window_triggers: tuple[str, ...] = field(default_factory=tuple)


def _render(content: str) -> str:
    """Substitute path placeholders. str.replace, NOT str.format — playbook
    bodies contain JSON examples with braces that format() would choke on."""
    for var, value in _PATH_VARS.items():
        content = content.replace(var, value)
    return content


_cache: tuple[float, list[Playbook]] | None = None  # (dir mtime, books)


def load_all() -> list[Playbook]:
    """Load every playbooks/*.json, cached on directory mtime.

    for_window_title runs every agent step — without the cache that would be
    O(steps × files) disk reads. Editing/adding a JSON bumps the dir mtime on
    Windows, which invalidates the cache automatically.
    """
    global _cache
    if not PLAYBOOKS_DIR.exists():
        return []
    try:
        mtime = max([PLAYBOOKS_DIR.stat().st_mtime]
                    + [p.stat().st_mtime for p in PLAYBOOKS_DIR.glob("*.json")])
    except OSError:
        mtime = 0.0
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    books: list[Playbook] = []
    for path in sorted(PLAYBOOKS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            books.append(Playbook(
                name=str(data.get("name") or path.stem),
                description=str(data.get("description", "")),
                content=_render(str(data["content"])),
                keywords=tuple(str(k).lower() for k in data.get("keywords", [])),
                window_triggers=tuple(str(t) for t in data.get("window_triggers", [])),
            ))
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("playbook %s 加载失败: %s", path.name, exc)
    _cache = (mtime, books)
    return books


def enabled() -> bool:
    return bool(CONFIG.get("agent", "playbooks_enabled", default=True))


def for_task(task: str) -> list[Playbook]:
    """Playbooks whose keywords appear in the task text.

    When the playbook switch is off, return everything — equivalent to the
    old always-in-prompt behavior.
    """
    books = load_all()
    if not enabled():
        return books
    task_lower = (task or "").lower()
    return [b for b in books if any(k in task_lower for k in b.keywords)]


def for_window_title(title: str, exclude: set[str]) -> Playbook | None:
    """Playbook whose runtime trigger matches a window title (mid-run rescue
    for tasks the start-time keywords didn't anticipate).

    When several triggers match, the LONGEST (most specific) one wins — e.g.
    "你要如何打开此文件" must hit the media chooser playbook via "你要如何打开",
    not the file-dialog playbook via the broad "打开".
    """
    if not enabled() or not title:
        return None
    best: tuple[int, Playbook] | None = None
    for book in load_all():
        if book.name in exclude:
            continue
        for trigger in book.window_triggers:
            if trigger in title and (best is None or len(trigger) > best[0]):
                best = (len(trigger), book)
    return best[1] if best else None


def render_section(books: list[Playbook]) -> str:
    """Concatenated playbook bodies for prompt injection ('' when none)."""
    if not books:
        return ""
    return "\n\n" + "\n\n".join(b.content for b in books)
