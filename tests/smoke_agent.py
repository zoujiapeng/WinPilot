"""Smoke test: agent layer + trace export/replay, no API calls, no side effects.

Covers: imports, tool schemas, system prompt, ToolSession read-only dispatch
(list_windows/observe/read_text/color_probe), AgentRunner history folding,
experience memory round-trip, trace export + harmless replay (wait-only step).
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winpilot.agent import prompts  # noqa: E402
from winpilot.agent.loop import AgentRunner  # noqa: E402
from winpilot.agent.tools import ToolSession, tool_schemas  # noqa: E402
from winpilot.config import TRACES_DIR  # noqa: E402
from winpilot.memory import experience  # noqa: E402
from winpilot.perception import win32  # noqa: E402
from winpilot.recorder import trace as trace_mod  # noqa: E402

# --- tool schemas -----------------------------------------------------------
schemas = tool_schemas()
names = [t["function"]["name"] for t in schemas]
print("tools:", names)
for required in ("observe", "click", "type_text", "hotkey", "wait", "done", "fail"):
    assert required in names, f"缺少工具 {required}"
json.dumps(schemas)  # must be valid JSON-serializable

# --- system prompt ----------------------------------------------------------
sp = prompts.system_prompt(experience_hints="测试经验", vlm_enabled=False)
assert "expect" in sp and "observe" in sp and "测试经验" in sp
assert "vlm_describe" not in sp
sp_vlm = prompts.system_prompt(vlm_enabled=True)
assert "vlm_describe" in sp_vlm
print("system prompt ok, len =", len(sp))

# --- ToolSession read-only dispatch ----------------------------------------
session = ToolSession()
res = session.dispatch("list_windows", {})
assert "hwnd=" in res.text, res.text
print("list_windows ok:", res.text.splitlines()[0])

windows = win32.list_windows()
hwnd = windows[0]["hwnd"]
res = session.dispatch("observe", {"hwnd": hwnd, "include_icons": False})
assert res.is_observe and session.snapshot is not None
print("observe ok:", res.text.splitlines()[0], f"({len(session.snapshot.elements)}元素)")
assert res.summary.startswith("[已折叠]")

res = session.dispatch("read_text", {"hwnd": hwnd})
print("read_text ok:", len(res.text), "chars")

res = session.dispatch("color_probe", {"x": 10, "y": 10})
assert "average_hex" in res.text
print("color_probe ok:", res.text)

res = session.dispatch("done", {"summary": "测试"})
assert res.terminal == "done"
res = session.dispatch("nonexistent_tool", {})
assert "未知工具" in res.text

# --- AgentRunner folding logic (threshold-gated, no API) --------------------
runner = AgentRunner("测试任务")
runner.messages = [{"role": "system", "content": "s"}]
# Small content stays unfolded → keeps DeepSeek prefix cache warm.
for i in range(4):
    runner._append_tool_result(f"id{i}", f"小观察{i}", summary=f"[已折叠] observe #{i}")
folded_small = [m for m in runner.messages if m["content"].startswith("[已折叠]")]
assert len(folded_small) == 0, f"小上下文不应折叠，却折了 {len(folded_small)}"
print("threshold folding ok: 小上下文 0 折叠（缓存友好）")

# Large content over threshold → oldest fold, newest 2 stay full.
runner2 = AgentRunner("大任务")
runner2.messages = [{"role": "system", "content": "s"}]
big = "大段观察内容" * 4000  # ~24K chars each, well over 48K total after 3
for i in range(4):
    runner2._append_tool_result(f"id{i}", big, summary=f"[已折叠] observe #{i}")
folded_big = [m for m in runner2.messages if m["content"].startswith("[已折叠]")]
full_big = [m for m in runner2.messages
            if m["role"] == "tool" and not m["content"].startswith("[已折叠]")]
assert len(full_big) == 2, f"应保留最近2个完整，实际 {len(full_big)}"
assert len(folded_big) == 2, f"应折叠最旧2个，实际 {len(folded_big)}"
print("threshold folding ok: 大上下文 2 完整 + 2 折叠")

# --- experience memory round-trip -------------------------------------------
experience.record("冒烟测试任务ABC", "记事本", ["launch(notepad)", "type_text(hi)"])
hints = experience.hints_for("冒烟测试任务ABC")
assert "launch(notepad)" in hints, hints
recipes = experience.all_recipes()
assert any(r["task"] == "冒烟测试任务ABC" for r in recipes)
# cleanup our test entry
remaining = [r for r in recipes if r["task"] != "冒烟测试任务ABC"]
experience._save(remaining)
print("experience memory ok")

# --- trace export + harmless replay ------------------------------------------
run_id = "smoketest-" + time.strftime("%H%M%S")
TRACES_DIR.mkdir(exist_ok=True)
title_needle = windows[0]["title"][:8]
events = [
    {"type": "agent_start", "ts": time.time(), "task": "冒烟导出测试", "run_id": run_id},
    {"type": "agent_tool", "ts": time.time(), "tool": "wait", "run_id": run_id,
     "replay": {"tool": "wait", "condition": {"window_appears": title_needle}, "timeout_s": 3}},
    {"type": "agent_done", "ts": time.time(), "status": "done", "run_id": run_id},
]
with open(TRACES_DIR / f"{run_id}.jsonl", "w", encoding="utf-8") as fh:
    for e in events:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")

assert any(t["run_id"] == run_id for t in trace_mod.list_traces())
script_path = trace_mod.export_script(run_id, name=run_id)
print("export ok:", script_path.name)

result = trace_mod.replay(script_path, stop_event=threading.Event())
print("replay result:", result)
assert result["ok"], result

# cleanup
script_path.unlink()
(TRACES_DIR / f"{run_id}.jsonl").unlink()
import shutil
shutil.rmtree(TRACES_DIR / run_id, ignore_errors=True)

print("SMOKE AGENT OK")
