"""Lightweight world model: minimal cross-step state for stuck detection and
change awareness — WITHOUT any extra perception cost.

It consumes only the snapshots and actions the agent already produces (no new
screen captures) and surfaces a short advisory *only* when something is worth
telling the model: an action that changed nothing, or repeated identical
actions. Deliberately tiny — this is decision-support state, not a knowledge
graph. The negative-optimization guard for this module is: never call into
perception here, and stay silent when there is nothing noteworthy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# Tools whose repetition / no-visible-effect signals being stuck. Intentionally
# excludes read-only tools (observe/read_text/list_windows/find_app/search_files/
# color_probe/find_image/vlm_describe) and ``wait`` (legitimately changes nothing).
_ACTION_TOOLS = {
    "launch", "click", "type_text", "hotkey", "scroll", "drag",
    "focus_window", "tray_click", "run_shell",
}
_STUCK_REPEAT = 3        # same action this many times in a row -> stuck
_STUCK_NO_CHANGE = 3     # this many post-action observes with no change -> stuck
_MAX_HISTORY = 12
_MAX_DELTA_ITEMS = 4     # how many added/removed element names to surface


def _args_hash(name: str, args: dict) -> str:
    compact = {k: v for k, v in (args or {}).items() if k != "expect"}
    blob = name + "|" + json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]  # noqa: S324 (non-crypto id)


@dataclass(frozen=True)
class _ObserveState:
    title: str
    fingerprint: frozenset[str]
    count: int


class WorldModel:
    """Per-run, in-memory only. Fed by ToolSession.dispatch; read by the loop."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, str]] = []   # (tool_name, args_hash)
        self._last_observe: _ObserveState | None = None
        self._identical_observes = 0
        self._action_since_observe = False
        self._last_change_line = ""
        self._stuck_announced = False                # edge-trigger guard for replan

    # ---------------------------------------------------------------- feed
    @staticmethod
    def _fingerprint(snapshot) -> frozenset[str]:
        """Structural signature of a snapshot: the set of text-bearing elements.

        Position-only changes (e.g. a moving progress bar) intentionally do not
        register — stuck detection cares about structural progress.
        """
        fp: set[str] = set()
        for element in snapshot.elements:
            text = (getattr(element, "text", "") or "").strip()
            if text:
                fp.add(f"{element.role}:{text}")
        return frozenset(fp)

    def record_action(self, name: str, args: dict) -> None:
        if name not in _ACTION_TOOLS:
            return
        self._action_since_observe = True
        self._actions.append((name, _args_hash(name, args)))
        if len(self._actions) > _MAX_HISTORY:
            self._actions = self._actions[-_MAX_HISTORY:]
        # NOTE: do NOT re-arm the stuck signal here. It re-arms only on a real
        # structural screen change (observe_snapshot), so a replan that swaps to
        # a different action but still makes no progress can't re-fire the
        # structured-replan prompt every step.

    def observe_snapshot(self, snapshot) -> None:
        fingerprint = self._fingerprint(snapshot)
        prev = self._last_observe
        acted = self._action_since_observe
        self._action_since_observe = False

        same = (
            prev is not None
            and prev.title == snapshot.title
            and prev.fingerprint == fingerprint
        )
        if same:
            # Only count "no change" against progress when an action happened
            # between the two observes (back-to-back observes are not stuck).
            if acted:
                self._identical_observes += 1
                self._last_change_line = "动作后界面无可见变化"
        else:
            self._identical_observes = 0
            self._last_change_line = self._describe_delta(prev, snapshot.title, fingerprint)
            self._stuck_announced = False  # the screen changed = progress
        self._last_observe = _ObserveState(snapshot.title, fingerprint, len(snapshot.elements))

    @staticmethod
    def _describe_delta(prev: _ObserveState | None, title: str,
                        fingerprint: frozenset[str]) -> str:
        if prev is None:
            return ""
        parts: list[str] = []
        if prev.title != title:
            parts.append(f'标题 "{prev.title}"→"{title}"')
        added = sorted(t for t in fingerprint - prev.fingerprint)[:_MAX_DELTA_ITEMS]
        gone = sorted(t for t in prev.fingerprint - fingerprint)[:_MAX_DELTA_ITEMS]
        if added:
            parts.append("新增 " + ", ".join(added))
        if gone:
            parts.append("消失 " + ", ".join(gone))
        return "；".join(parts)

    # ---------------------------------------------------------------- read
    def repeated_action_count(self) -> int:
        """How many times the most recent action repeats consecutively."""
        if not self._actions:
            return 0
        last = self._actions[-1]
        count = 0
        for item in reversed(self._actions):
            if item == last:
                count += 1
            else:
                break
        return count

    def is_stuck(self) -> tuple[bool, str]:
        repeats = self.repeated_action_count()
        if repeats >= _STUCK_REPEAT:
            return True, f"连续 {repeats} 次重复同一动作，状态未推进"
        if self._identical_observes >= _STUCK_NO_CHANGE:
            return True, f"连续 {self._identical_observes} 次动作后界面均无变化"
        return False, ""

    def take_stuck_signal(self) -> tuple[bool, str]:
        """Edge-triggered stuck signal for forcing a structured replan: returns
        (True, why) only the FIRST time stuck is detected in an episode, then
        stays quiet until progress resets it (avoids replan-spam every step)."""
        stuck, why = self.is_stuck()
        if stuck and not self._stuck_announced:
            self._stuck_announced = True
            return True, why
        return False, ""

    def advisory(self) -> str:
        """Mild, non-blocking hint surfaced to the model; empty when nothing
        noteworthy. (Hard 'stuck' handling is edge-triggered via
        take_stuck_signal → structured replan in the loop.)"""
        if self._identical_observes >= 2 and self._last_change_line:
            return f"提示：{self._last_change_line}，请确认上一步是否点对了目标。"
        return ""

    def state(self) -> dict:
        """Compact state for the UI / trace (not sent to the LLM)."""
        return {
            "actions": len(self._actions),
            "repeated": self.repeated_action_count(),
            "identical_observes": self._identical_observes,
            "last_change": self._last_change_line,
            "last_title": self._last_observe.title if self._last_observe else "",
        }
