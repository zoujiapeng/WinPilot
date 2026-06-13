"""Experience memory: task recipes with ids, scoring, and CRUD.

Hardening (Phase 2-E/F):
- every recipe has a stable ``id`` → single-item CRUD via API/UI/agent tools
- ``provisional`` flag: a recipe recorded from a run whose actions were never
  externally verified is marked unverified and rendered with a warning
- score tracking: when injected recipes lead to success/failure the counters
  update; recipes that keep failing are evicted (bad experience must not
  poison future runs)
- steps are intent-level strings (built in loop._record_step from replay
  metadata) instead of raw coordinate-bound tool calls

Stored as a small JSON file — human-editable, easy to clear.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

from ..config import CONFIG, MEMORY_PATH
from ..utils.log import logger

_MAX_RECIPES = 50
_MAX_HINTS = 3
# Evict a recipe once it has been tried this many times without ever helping.
_EVICT_MIN_USES = 3
_lock = threading.Lock()


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _normalize(recipe: dict) -> dict:
    """Backfill fields for recipes written by older versions."""
    recipe.setdefault("id", _new_id())
    recipe.setdefault("uses", 0)
    recipe.setdefault("successes", 0)
    recipe.setdefault("provisional", False)
    recipe.setdefault("manual", False)
    recipe.setdefault("script", "")  # linked .wps.json for 0-token reuse
    return recipe


# Old-format step strings like `click({"element_id": 39})` / `focus_window({"hwnd": 263756})`
# encode per-run handles that are meaningless next session — they can't be reused.
_JUNK_STEP = re.compile(r'\b(element_id|hwnd)\b')
_OLD_FORMAT = re.compile(r'^\w+\(\{.*\}\)$')


def _clean_steps(steps: list) -> list[str]:
    """Drop legacy steps that bind to per-run element_id/hwnd (un-reusable)."""
    cleaned = []
    for s in steps:
        s = str(s)
        if _OLD_FORMAT.match(s.strip()) and _JUNK_STEP.search(s):
            continue
        cleaned.append(s)
    return cleaned


def _load() -> list[dict]:
    if not MEMORY_PATH.exists():
        return []
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        recipes, seen = [], set()
        for r in data:
            r = _normalize(r)
            if r["id"] in seen:  # enforce id uniqueness (race-written dupes)
                continue
            r["steps"] = _clean_steps(r.get("steps", []))
            if not r["steps"] and not r.get("manual"):
                continue  # legacy recipe with nothing reusable left → drop
            seen.add(r["id"])
            recipes.append(r)
        return recipes
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


# ------------------------------------------------------------------ recording
def record(task: str, app_hint: str, steps: list[str],
           verified: bool = True, script: str = "") -> None:
    """Persist a recipe from a finished run (newest first, capped).

    ``verified=False`` marks the recipe provisional. ``script`` links a
    replayable .wps.json for 0-token reuse.
    """
    if not enabled() or not steps:
        return
    recipe = _normalize({
        "task": task[:200],
        "app": app_hint[:80],
        "steps": steps[:30],
        "ts": int(time.time()),
        "provisional": not verified,
        "script": script,
    })
    with _lock:
        recipes = [r for r in _load() if r.get("task") != recipe["task"]]
        recipes.insert(0, recipe)
        _save(recipes[:_MAX_RECIPES])
    logger.info("经验记忆已保存: %s (%d步%s%s)", task[:40], len(steps),
                "，未验证" if not verified else "",
                "，含可复用脚本" if script else "")


def feedback(recipe_ids: list[str], success: bool) -> None:
    """Update scores for recipes that were injected into a finished run.

    A success also promotes provisional recipes (they proved useful); recipes
    that keep failing are evicted so bad experience cannot keep poisoning runs.
    """
    if not recipe_ids:
        return
    ids = set(recipe_ids)
    with _lock:
        recipes = _load()
        kept: list[dict] = []
        for r in recipes:
            if r["id"] in ids:
                r["uses"] += 1
                if success:
                    r["successes"] += 1
                    r["provisional"] = False
                if r["uses"] >= _EVICT_MIN_USES and r["successes"] == 0:
                    logger.info("经验配方屡试无效，已淘汰: %s", r.get("task", "")[:40])
                    continue
            kept.append(r)
        _save(kept)


# ------------------------------------------------------------------ matching
def hints_with_ids(task: str) -> tuple[str, list[str]]:
    """Render up to N relevant recipes + their ids (for score feedback)."""
    if not enabled():
        return "", []
    task_lower = task.lower()
    words = [w for w in task_lower.replace("，", " ").replace(",", " ").split() if len(w) >= 2]
    scored: list[tuple[float, dict]] = []
    for recipe in _load():
        haystack = (recipe.get("task", "") + recipe.get("app", "")).lower()
        score: float = sum(1 for w in words if w in haystack)
        # Chinese tasks rarely have spaces — substring overlap fallback.
        # >=2 bigram hits count (a single 2-char hit is too weak a signal).
        if not score and len(task_lower) >= 4:
            matches = sum(
                1 for i in range(len(task_lower) - 1)
                if task_lower[i : i + 2] in haystack
            )
            score = matches / 3.0 if matches >= 2 else 0.0
        if score > 0:
            # Proven recipes outrank unproven; never-successful ones sink.
            uses, wins = recipe["uses"], recipe["successes"]
            if wins > 0:
                score += min(wins, 3) * 0.5
            elif uses > 0:
                score -= uses * 0.5
            if recipe.get("provisional"):
                score -= 0.5
            if score > 0:
                scored.append((score, recipe))
    scored.sort(key=lambda item: item[0], reverse=True)
    blocks, ids = [], []
    for rank, (score, recipe) in enumerate(scored[:_MAX_HINTS]):
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(recipe["steps"]))
        # Strong match + linked replay script → promote to "reusable procedure".
        strong = rank == 0 and recipe.get("script") and _strong_match(task, recipe["task"])
        if strong:
            blocks.append(
                f'⭐ 上次成功流程（匹配度高，可直接复用）"{recipe["task"]}" (应用:{recipe["app"]})：\n'
                f'{steps}\n'
                f'  → 若当前任务与此基本相同，可调用 reuse_skill("{recipe["task"]}") '
                f'直接 0-token 重放上次步骤；失败会自动回退到实时操作。')
        else:
            tag = "（未验证，谨慎参考）" if recipe.get("provisional") else ""
            blocks.append(f'任务"{recipe["task"]}"{tag} (应用:{recipe["app"]}):\n{steps}')
        ids.append(recipe["id"])
    return "\n".join(blocks), ids


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _strong_match(query: str, recipe_task: str) -> bool:
    """High-confidence 'same task': normalized containment or high bigram overlap."""
    a, b = _norm(query), _norm(recipe_task)
    if len(a) < 3 or len(b) < 3:
        return False  # too short → containment would false-positive
    if a == b or a in b or b in a:
        return True
    ba = {a[i:i+2] for i in range(len(a) - 1)}
    bb = {b[i:i+2] for i in range(len(b) - 1)}
    if not ba or not bb:
        return False
    overlap = len(ba & bb) / min(len(ba), len(bb))
    return overlap >= 0.7


def reusable_script_for(task: str, recipe_id: str = "") -> tuple[str, dict | None]:
    """Resolve a task (or explicit recipe id) to its linked .wps.json script.

    Returns (script_filename, recipe) or ("", None). Used by the reuse_skill tool.
    """
    if recipe_id:
        recipe = get(recipe_id)
        if recipe and recipe.get("script"):
            return recipe["script"], recipe
        return "", recipe
    _text, ids = hints_with_ids(task)
    for rid in ids:  # best match first
        recipe = get(rid)
        if recipe and recipe.get("script"):
            return recipe["script"], recipe
    return "", None


def hints_for(task: str) -> str:
    """Back-compat wrapper used by older callers/tests."""
    return hints_with_ids(task)[0]


# ---------------------------------------------------------------------- CRUD
def all_recipes() -> list[dict]:
    return _load()


def get(recipe_id: str) -> dict | None:
    for r in _load():
        if r["id"] == recipe_id:
            return r
    return None


def add(task: str, app: str = "", steps: list[str] | None = None,
        manual: bool = True) -> dict:
    """Manually add a recipe/note (UI form or agent ``remember`` tool)."""
    recipe = _normalize({
        "task": (task or "")[:200],
        "app": (app or "")[:80],
        "steps": [str(s)[:300] for s in (steps or [])][:30],
        "ts": int(time.time()),
        "manual": manual,
    })
    with _lock:
        recipes = _load()
        recipes.insert(0, recipe)
        _save(recipes[:_MAX_RECIPES])
    return recipe


def update(recipe_id: str, patch: dict) -> dict | None:
    """Edit a single recipe; only whitelisted fields are touched."""
    allowed = {"task", "app", "steps", "provisional"}
    with _lock:
        recipes = _load()
        for r in recipes:
            if r["id"] == recipe_id:
                for key, value in patch.items():
                    if key not in allowed:
                        continue
                    if key == "steps":
                        r["steps"] = [str(s)[:300] for s in (value or [])][:30]
                    elif key == "provisional":
                        r["provisional"] = bool(value)
                    else:
                        r[key] = str(value)[:200]
                _save(recipes)
                return r
    return None


def delete(recipe_id: str) -> bool:
    with _lock:
        recipes = _load()
        kept = [r for r in recipes if r["id"] != recipe_id]
        if len(kept) == len(recipes):
            return False
        _save(kept)
    return True


def delete_matching(query: str) -> list[dict]:
    """Delete recipes whose task/app/steps contain the query (agent ``forget``)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    with _lock:
        recipes = _load()
        removed = [
            r for r in recipes
            if q in (r.get("task", "") + r.get("app", "")
                     + " ".join(r.get("steps", []))).lower()
        ]
        if removed:
            _save([r for r in recipes if r not in removed])
    return removed


def clear() -> None:
    with _lock:
        _save([])
