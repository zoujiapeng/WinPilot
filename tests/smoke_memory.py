"""Smoke test for experience memory CRUD + scoring + anti-bad-experience.

Uses a temporary memory file — never touches the real experience_memory.json.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.memory import experience  # noqa: E402

_tmp = Path(tempfile.mkdtemp()) / "mem_test.json"
experience.MEMORY_PATH = _tmp  # redirect storage for the whole test


def test_crud() -> None:
    experience.clear()
    r = experience.add("测试任务A", app="记事本", steps=["click 文件", "click 保存"])
    assert r["id"] and r["manual"]
    assert experience.get(r["id"])["task"] == "测试任务A"

    updated = experience.update(r["id"], {"task": "测试任务A改", "steps": ["仅一步"]})
    assert updated["task"] == "测试任务A改" and updated["steps"] == ["仅一步"]
    assert experience.update(r["id"], {"id": "hack", "uses": 99}) is not None
    assert experience.get(r["id"])["uses"] == 0, "白名单外字段不可改"

    assert experience.delete(r["id"]) is True
    assert experience.delete(r["id"]) is False
    print("[OK] CRUD 增查改删")


def test_provisional_and_promotion() -> None:
    experience.clear()
    experience.record("未验证的任务", "app", ["step1"], verified=False)
    rec = experience.all_recipes()[0]
    assert rec["provisional"] is True
    text, ids = experience.hints_with_ids("未验证的任务")
    assert "未验证" in text and ids == [rec["id"]]
    # success promotes provisional -> trusted
    experience.feedback(ids, success=True)
    rec2 = experience.get(rec["id"])
    assert rec2["provisional"] is False and rec2["successes"] == 1
    print("[OK] 未验证标记 + 成功后转正")


def test_eviction() -> None:
    experience.clear()
    experience.record("总是失败的配方", "app", ["bad step"], verified=True)
    rid = experience.all_recipes()[0]["id"]
    for _ in range(3):
        experience.feedback([rid], success=False)
    assert experience.get(rid) is None, "3 次注入均失败应被淘汰"
    print("[OK] 屡败配方淘汰")


def test_scoring_prefers_proven() -> None:
    experience.clear()
    experience.record("打开记事本写字", "记事本", ["s"], verified=True)
    proven_id = experience.all_recipes()[0]["id"]
    experience.record("打开记事本画画", "记事本", ["s"], verified=False)
    experience.feedback([proven_id], success=True)
    _text, ids = experience.hints_with_ids("打开记事本")
    assert ids[0] == proven_id, "成功过的配方应排前面"
    print("[OK] 评分排序：已验证成功者优先")


def test_forget_matching() -> None:
    experience.clear()
    experience.add("记住用户喜欢深色主题", app="备忘", steps=["深色主题"])
    experience.add("无关条目", app="x", steps=["y"])
    removed = experience.delete_matching("深色主题")
    assert len(removed) == 1 and len(experience.all_recipes()) == 1
    print("[OK] forget 按关键词删除")


def test_legacy_migration() -> None:
    experience.clear()
    # simulate an old-format file without id/score fields
    _tmp.write_text('[{"task":"旧条目","app":"a","steps":["s"],"ts":1}]',
                    encoding="utf-8")
    recs = experience.all_recipes()
    assert recs[0]["id"] and recs[0]["uses"] == 0 and recs[0]["provisional"] is False
    assert recs[0].get("script") == "", "新字段 script 应回填"
    print("[OK] 旧格式自动补 id/score/script 字段")


def test_legacy_junk_steps_dropped() -> None:
    experience.clear()
    # old per-run-handle steps must be purged; recipe with only junk → dropped
    _tmp.write_text(
        '[{"id":"j1","task":"垃圾配方","app":"x",'
        '"steps":["click({\\"element_id\\": 39})","focus_window({\\"hwnd\\": 263756})"],"ts":1},'
        '{"id":"k2","task":"好配方","app":"y",'
        '"steps":["click \\"保存\\"","click({\\"element_id\\": 5})"],"ts":2}]',
        encoding="utf-8")
    recs = experience.all_recipes()
    tasks = {r["task"]: r for r in recs}
    assert "垃圾配方" not in tasks, "纯垃圾配方应被丢弃"
    assert "好配方" in tasks and tasks["好配方"]["steps"] == ['click "保存"'], \
        "好配方应剥掉 element_id 步、保留意图步"
    print("[OK] 旧格式垃圾步骤清理")


def test_script_link_and_strong_match() -> None:
    experience.clear()
    experience.record("打开计算器算123", "计算器", ["launch calc", "type 123"],
                      verified=True, script="run123.wps.json")
    rid = experience.all_recipes()[0]["id"]
    # promote on use so it's a strong match
    experience.feedback([rid], success=True)
    experience.feedback([rid], success=True)
    text, ids = experience.hints_with_ids("打开计算器算123")
    assert "⭐" in text and "reuse_skill" in text, "强匹配+脚本应升级为可复用提示"
    script, recipe = experience.reusable_script_for("打开计算器算123")
    assert script == "run123.wps.json" and recipe["id"] == rid
    # no-script task → no reusable
    s2, _ = experience.reusable_script_for("完全无关的任务xyz")
    assert s2 == ""
    print("[OK] 脚本链接 + 强匹配复用提示 + reusable_script_for")


if __name__ == "__main__":
    test_crud()
    test_provisional_and_promotion()
    test_eviction()
    test_scoring_prefers_proven()
    test_forget_matching()
    test_legacy_migration()
    test_legacy_junk_steps_dropped()
    test_script_link_and_strong_match()
    print("\n记忆系统 smoke 全部通过 ✓")
