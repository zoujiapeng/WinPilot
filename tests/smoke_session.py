"""Smoke test for session continuity (cross-task memory within a session)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.agent import session as session_mod  # noqa: E402
from winpilot.agent.session import SESSION, SessionMemory  # noqa: E402


def test_ring_and_recent() -> None:
    sm = SessionMemory()
    for i in range(12):
        sm.append(f"任务{i}", "done", f"结果{i}", f"run{i}")
    recent = sm.recent(3)
    assert len(recent) == 3 and recent[-1].task == "任务11", "recent 应取最新3条"
    assert len(sm.all()) <= 8, "环形缓冲应封顶"
    assert sm.all()[0]["task"] == "任务11", "all() 最新在前"
    print("[OK] 环形缓冲 + recent")


def test_render_context_resolves_reference() -> None:
    sm = SessionMemory()
    sm.append("打开计算器", "done", "计算器已打开", "r1")
    ctx = session_mod.render_context(sm.recent())
    assert "打开计算器" in ctx and "计算器已打开" in ctx
    assert "再来一次" in ctx and "最后一条" in ctx, "应给出指代消解提示"
    assert session_mod.render_context([]) == "", "空会话不注入"
    print("[OK] 会话上下文渲染含指代提示")


def test_clear() -> None:
    sm = SessionMemory()
    sm.append("x", "done", "y", "r")
    sm.clear()
    assert sm.all() == [] and sm.recent() == []
    print("[OK] 清空会话")


def test_singleton_exists() -> None:
    assert isinstance(SESSION, SessionMemory)
    print("[OK] SESSION 单例存在")


if __name__ == "__main__":
    test_ring_and_recent()
    test_render_context_resolves_reference()
    test_clear()
    test_singleton_exists()
    print("\n会话连续性 smoke 全部通过 ✓")
