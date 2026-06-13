"""Session memory: cross-task continuity within one server lifetime.

The agent runs each task as a fresh AgentRunner with empty history, so a
follow-up like "再来一次 / 把它关掉 / 刚才那个" has no antecedent and the agent
acts as if starting a brand-new conversation. This module keeps a short log of
recently finished tasks (this session only) so referential commands resolve.

Process-lifetime singleton = one "session"; a server restart clears it, which
matches the intuitive notion of a session boundary. Not persisted to disk.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

_MAX_ENTRIES = 8
_RECENT_FOR_PROMPT = 3
_SUMMARY_CHARS = 160


@dataclass(frozen=True)
class SessionEntry:
    task: str
    status: str
    result: str
    run_id: str


class SessionMemory:
    """Thread-safe ring of recent finished tasks (newest last)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[SessionEntry] = []

    def append(self, task: str, status: str, result: str, run_id: str) -> None:
        entry = SessionEntry(
            task=(task or "")[:200],
            status=status or "",
            result=(result or "")[:_SUMMARY_CHARS],
            run_id=run_id or "",
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > _MAX_ENTRIES:
                self._entries = self._entries[-_MAX_ENTRIES:]

    def recent(self, n: int = _RECENT_FOR_PROMPT) -> list[SessionEntry]:
        with self._lock:
            return list(self._entries[-n:])

    def all(self) -> list[dict]:
        with self._lock:
            return [
                {"task": e.task, "status": e.status, "result": e.result,
                 "run_id": e.run_id}
                for e in reversed(self._entries)  # newest first for UI
            ]

    def clear(self) -> None:
        with self._lock:
            self._entries = []


SESSION = SessionMemory()


def render_context(entries: list[SessionEntry]) -> str:
    """Compact prompt block ('' when empty). Injected at the history tail."""
    if not entries:
        return ""
    lines = ["【本会话此前已完成的任务（从旧到新）】"]
    for i, e in enumerate(entries, start=1):
        summary = e.result.replace("\n", " ").strip()
        lines.append(f'{i}. "{e.task}" → {e.status}：{summary}')
    lines.append(
        "若当前指令含'再来一次/再来/重复/刚才那个/那个/把它关了/继续'等指代，"
        "通常指上面【最后一条】任务——据此理解并执行，必要时仍先 observe 确认当前界面。"
    )
    return "\n".join(lines)
