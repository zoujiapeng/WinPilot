"""Smoke test for the lightweight world model (pure logic, no GUI / no API)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.perception.world import WorldModel  # noqa: E402


@dataclass
class _FakeElem:
    role: str
    text: str


@dataclass
class _FakeSnapshot:
    title: str
    elements: list


def _snap(title: str, *pairs: tuple[str, str]) -> _FakeSnapshot:
    return _FakeSnapshot(title=title, elements=[_FakeElem(r, t) for r, t in pairs])


def test_repeated_action_is_stuck() -> None:
    wm = WorldModel()
    for _ in range(2):
        wm.record_action("click", {"x": 10, "y": 20})
    assert not wm.is_stuck()[0], "2 次重复还不该判定卡死"
    wm.record_action("click", {"x": 10, "y": 20})
    stuck, why = wm.is_stuck()
    assert stuck and "重复" in why, f"3 次重复应判卡死: {why}"
    assert wm.take_stuck_signal()[0], "卡死应可被结构化replan信号取到"
    print("[OK] 重复动作卡死检测")


def test_different_actions_not_stuck() -> None:
    wm = WorldModel()
    wm.record_action("click", {"x": 1, "y": 1})
    wm.record_action("type_text", {"text": "a"})
    wm.record_action("click", {"x": 2, "y": 2})
    assert not wm.is_stuck()[0], "不同动作不应判卡死"
    assert wm.advisory() == ""
    print("[OK] 不同动作不误判")


def test_no_change_after_action() -> None:
    wm = WorldModel()
    wm.observe_snapshot(_snap("记事本", ("button", "文件"), ("button", "编辑")))
    # act then observe identical -> one no-progress signal (advisory fires at >=2)
    wm.record_action("click", {"x": 5, "y": 5})
    wm.observe_snapshot(_snap("记事本", ("button", "文件"), ("button", "编辑")))
    wm.record_action("click", {"x": 6, "y": 6})
    wm.observe_snapshot(_snap("记事本", ("button", "文件"), ("button", "编辑")))
    assert "点对" in wm.advisory(), f"两次动作后无变化应给温和提示: {wm.advisory()!r}"
    # two more no-change cycles -> stuck
    for _ in range(2):
        wm.record_action("click", {"x": 5, "y": 5})
        # vary args so repeat-detector doesn't fire; isolate no-change path
        wm.record_action("hotkey", {"keys": ["enter"]})
        wm.observe_snapshot(_snap("记事本", ("button", "文件"), ("button", "编辑")))
    stuck, why = wm.is_stuck()
    assert stuck and "无变化" in why, f"多次动作后无变化应判卡死: {why}"
    print("[OK] 动作后无变化卡死检测")


def test_change_resets_and_describes() -> None:
    wm = WorldModel()
    wm.observe_snapshot(_snap("记事本", ("button", "文件")))
    wm.record_action("hotkey", {"keys": ["ctrl", "s"]})
    wm.observe_snapshot(_snap("另存为", ("button", "保存"), ("edit", "文件名")))
    assert not wm.is_stuck()[0], "界面变化后不应卡死"
    state = wm.state()
    assert "另存为" in state["last_change"] or "新增" in state["last_change"]
    assert wm.advisory() == "", "正常推进时应保持安静（不打扰模型）"
    print("[OK] 界面变化重置+描述+安静")


def test_back_to_back_observe_not_flagged() -> None:
    wm = WorldModel()
    wm.observe_snapshot(_snap("窗口", ("text", "x")))
    wm.observe_snapshot(_snap("窗口", ("text", "x")))  # no action between
    assert wm.advisory() == "", "无动作的连续 observe 不应报无变化"
    print("[OK] 无动作连续observe不误报")


def test_stuck_signal_edge_triggered() -> None:
    wm = WorldModel()
    for _ in range(3):
        wm.record_action("click", {"x": 1, "y": 1})
    assert wm.take_stuck_signal()[0], "首次卡死应触发"
    assert not wm.take_stuck_signal()[0], "同一卡死episode不应重复触发(防刷屏)"
    # merely switching actions without real progress must NOT re-arm (anti-spam)
    wm.record_action("hotkey", {"keys": ["enter"]})
    for _ in range(3):
        wm.record_action("drag", {"x": 9, "y": 9})
    assert not wm.take_stuck_signal()[0], "无真实进展(界面没变)不应重复触发"
    # a real structural screen change = progress → re-arms the signal
    wm.observe_snapshot(_snap("新窗口", ("button", "新内容")))
    for _ in range(3):
        wm.record_action("scroll", {"x": 2, "y": 2})
    assert wm.take_stuck_signal()[0], "界面真变化后的新卡死应再次触发"
    print("[OK] take_stuck_signal 边沿触发+仅真实进展重置")


if __name__ == "__main__":
    test_repeated_action_is_stuck()
    test_different_actions_not_stuck()
    test_no_change_after_action()
    test_change_resets_and_describes()
    test_back_to_back_observe_not_flagged()
    test_stuck_signal_edge_triggered()
    print("\n世界模型 smoke 全部通过 ✓")
