"""Experience memory: successful task recipes, keyed by application.

When enabled (agent.experience_memory), recipes matching the task text are
injected into the system prompt so the agent reuses proven step sequences.
Stored as a small JSON file — human-editable, easy to clear.
"""
from __future__ import annotations

import json
import os
import threading
import time

from ..config import CONFIG, MEMORY_PATH
from ..utils.log import logger

_MAX_RECIPES = 50
_MAX_HINTS = 3
_lock = threading.Lock()


def _load() -> list[dict]:
    if not MEMORY_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("经验记忆读取失败: %s", exc)
        return []


def _save(recipes: list[dict]) -> None:
    """Atomic write: a crash mid-save must not corrupt the memory file."""
    tmp = MEMORY_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(recipes, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MEMORY_PATH)


def enabled() -> bool:
    return bool(CONFIG.get("agent", "experience_memory", default=True))


def record(task: str, app_hint: str, steps: list[str]) -> None:
    """Persist a successful recipe (newest first, capped)."""
    if not enabled() or not steps:
        return
    recipe = {
        "task": task[:200],
        "app": app_hint[:80],
        "steps": steps[:30],
        "ts": int(time.time()),
    }
    with _lock:
        recipes = [r for r in _load() if r.get("task") != recipe["task"]]
        recipes.insert(0, recipe)
        _save(recipes[:_MAX_RECIPES])
    logger.info("经验记忆已保存: %s (%d步)", task[:40], len(steps))


def hints_for(task: str) -> str:
    """Render up to N relevant recipes as prompt text ('' when none/disabled)."""
    if not enabled():
        return ""
    task_lower = task.lower()
    words = [w for w in task_lower.replace("，", " ").replace(",", " ").split() if len(w) >= 2]
    scored: list[tuple[int, dict]] = []
    for recipe in _load():
        haystack = (recipe.get("task", "") + recipe.get("app", "")).lower()
        score = sum(1 for w in words if w in haystack)
        # Chinese tasks rarely have spaces — substring overlap fallback
        if not score and len(task_lower) >= 4:
            score = sum(
                1 for i in range(len(task_lower) - 1)
                if task_lower[i : i + 2] in haystack
            ) // 3
        if score > 0:
            scored.append((score, recipe))
    scored.sort(key=lambda item: item[0], reverse=True)
    blocks = []
    for _score, recipe in scored[:_MAX_HINTS]:
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(recipe["steps"]))
        blocks.append(f'任务"{recipe["task"]}" (应用:{recipe["app"]}):\n{steps}')
    return "\n".join(blocks)


def all_recipes() -> list[dict]:
    return _load()


def clear() -> None:
    with _lock:
        _save([])
