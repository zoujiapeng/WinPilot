"""Planning / todo layer: optional task decomposition for long-horizon tasks.

Design (negative-optimization guards):
- Upfront planning fires only for genuinely multi-step tasks (conservative
  heuristic); trivial tasks like "计算器 12×34" skip the extra LLM call.
- The plan is injected ONCE into the stable context prefix (cache-friendly);
  progress is carried forward by ``complete_subgoal`` tool results in the
  append-only history — the plan block is never rewritten per step.
- The model can also self-initiate / revise a plan mid-task via ``revise_plan``.
"""
from __future__ import annotations

import json
import re

from ..config import CONFIG
from ..utils.log import logger

# Valid keys for a subgoal completion check (mirror verify.SUPPORTED_CONDITIONS;
# imported lazily to avoid an import cycle at module load).
def _valid_check(check) -> dict | None:
    """Keep a check only if it's a dict of recognized verify conditions —
    an unknown key would make complete_subgoal a permanent blocker."""
    if not isinstance(check, dict) or not check:
        return None
    try:
        from ..action.verify import SUPPORTED_CONDITIONS
        known = set(SUPPORTED_CONDITIONS)
    except Exception:
        return check  # can't validate → trust it
    if all(k in known for k in check):
        return check
    logger.warning("规划 check 含未知条件 %s，已忽略该子目标的核验", list(check))
    return None

_MAX_STEPS = 6
_MAX_GOAL_CHARS = 60
# Sequence markers that hint at a multi-step task.
_SEQ_MARKERS = ("然后", "再", "接着", "之后", "最后", "依次", "并", "，", ",", "、", ";", "；", "\n")

_PLAN_SYSTEM = (
    "你是任务规划助手。把用户的 Windows 操作任务拆解为 2-6 个有序、可独立验证的子目标。"
    "输出一个 JSON 数组。每项可以是一句简短中文子目标(动宾短语)，"
    "或一个对象 {\"goal\":\"子目标\", \"check\":{验证条件}} —— 当某子目标有【确定性完成信号】时"
    "尽量带上 check(用于客观核验是否真完成)，可用键: "
    '{"file_exists":"路径"} {"file_contains":{"path":"..","text":".."}} '
    '{"process_running":"exe名"} {"window_appears":"标题"} {"text_appears":"界面文字"}。'
    "无确定性信号的子目标用纯字符串即可，不要编造 check。不要编号、不要解释。"
    '例: ["打开记事本", {"goal":"另存为到桌面a.txt", "check":{"file_exists":"C:\\\\Users\\\\me\\\\Desktop\\\\a.txt"}}]'
)


def planning_enabled() -> bool:
    return bool(CONFIG.get("agent", "planning_enabled", default=True))


def should_plan_upfront(task: str) -> bool:
    """Conservative: only auto-plan tasks that clearly span multiple steps."""
    if not planning_enabled():
        return False
    text = (task or "").strip()
    if len(text) >= 45:
        return True
    # Count total occurrences (not mere presence) so "a,b,c,d"-style short
    # enumerations don't trigger an unnecessary planning call.
    segments = sum(text.count(marker) for marker in _SEQ_MARKERS)
    return segments >= 4


def _parse_steps(raw: str) -> list[dict]:
    """Extract subgoals (each {goal, check}) from model output, tolerantly.

    Accepts items as plain strings or {"goal","check"} objects; coerces to a
    uniform list of dicts. Returns [] on failure.
    """
    if not raw:
        return []
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    blob = match.group(0) if match else raw
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            goal = str(item.get("goal", "")).strip()[:_MAX_GOAL_CHARS]
            check = _valid_check(item.get("check"))
        else:
            goal, check = str(item).strip()[:_MAX_GOAL_CHARS], None
        if goal:
            out.append({"goal": goal, "check": check})
    return out[:_MAX_STEPS]


def make_plan(client, model: str, task: str, temperature: float = 0.2) -> list[dict]:
    """One focused LLM call -> ordered subgoals (each {goal, check}). [] on failure."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": task},
            ],
            temperature=temperature,
            max_tokens=500,
        )
        steps = _parse_steps(resp.choices[0].message.content or "")
        logger.info("规划生成 %d 个子目标 (%d 个带核验)",
                    len(steps), sum(1 for s in steps if s.get("check")))
        return steps
    except Exception as exc:  # planning is best-effort; never block the run
        logger.warning("规划调用失败，退回无计划模式: %s", exc)
        return []


class Plan:
    """Ordered subgoals with completion flags + optional verification checks."""

    def __init__(self, steps: list) -> None:
        self.steps: list[dict] = []
        for s in steps:
            if isinstance(s, dict):
                self.steps.append({
                    "goal": str(s.get("goal", "")).strip()[:_MAX_GOAL_CHARS],
                    "done": False,
                    "check": _valid_check(s.get("check")),
                })
            else:
                self.steps.append({"goal": str(s).strip()[:_MAX_GOAL_CHARS],
                                   "done": False, "check": None})
        self.steps = [s for s in self.steps if s["goal"]]

    def active(self) -> bool:
        return bool(self.steps)

    def all_done(self) -> bool:
        return bool(self.steps) and all(s["done"] for s in self.steps)

    def check_for(self, index: int) -> dict | None:
        """The verification expect for a 1-based subgoal, if any."""
        if 1 <= index <= len(self.steps):
            return self.steps[index - 1].get("check")
        return None

    def complete(self, index: int) -> bool:
        """Mark a 1-based subgoal done. Returns False if index is invalid."""
        if 1 <= index <= len(self.steps):
            self.steps[index - 1]["done"] = True
            return True
        return False

    def render(self) -> str:
        lines = ["当前计划(todo)："]
        for i, step in enumerate(self.steps, start=1):
            mark = "x" if step["done"] else " "
            tag = " (含核验)" if step.get("check") else ""
            lines.append(f"[{mark}] {i}. {step['goal']}{tag}")
        lines.append(
            "完成一项就调用 complete_subgoal(index=序号)；计划不合适就调用 revise_plan 重列。"
        )
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        return [{"goal": s["goal"], "done": s["done"]} for s in self.steps]
