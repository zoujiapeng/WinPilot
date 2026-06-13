"""Smoke test for the eval runner: scorecard, baseline compare, ground-truth
checking — with the actual agent run stubbed out (no API spend / no GUI)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winpilot.eval import runner, tasks  # noqa: E402
from winpilot.eval.tasks import EvalTask  # noqa: E402


def test_runner_with_stub() -> None:
    # redirect results dir to temp
    runner.RESULTS_DIR = Path(tempfile.mkdtemp())
    runner.BASELINE_PATH = runner.RESULTS_DIR / "baseline.json"

    # stub the real agent run: pretend it claimed done with a result
    def fake_run_once(task: EvalTask) -> dict:
        return {"run_id": f"fake-{task.id}", "status": "done",
                "result_text": "结果是 408", "steps": 5,
                "usage": {"cost_cny": 0.02}, "elapsed_s": 3.0}

    runner._run_once = fake_run_once  # noqa: SLF001

    state = {"created": False}
    suite = [
        # passes: ground-truth check returns True
        EvalTask(id="pass_task", prompt="x", check=lambda c: (True, "ok")),
        # fails: ground-truth says not done even though agent claimed done
        EvalTask(id="fakedone_task", prompt="y", check=lambda c: (False, "文件不存在")),
        # uses result_text
        EvalTask(id="answer_task", prompt="算408", check=lambda c: ("408" in c["result_text"], "")),
    ]
    card = runner.run(suite, save_baseline=True)
    assert card["total"] == 3 and card["passed"] == 2, card
    assert abs(card["success_rate"] - 0.667) < 0.01
    # the fake-done task: agent said done but ground-truth FAIL — exactly the value
    res = {r["id"]: r for r in card["results"]}
    assert res["fakedone_task"]["agent_status"] == "done"
    assert res["fakedone_task"]["pass"] is False, "假done必须被ground-truth判负"
    print("[OK] runner 计分 + 假done被ground-truth判负 + result_text检查")

    # baseline compare on a second run (same numbers → deltas ~0)
    assert runner.BASELINE_PATH.exists()
    card2 = runner.run(suite)
    assert card2["success_rate"] == card["success_rate"]
    print("[OK] baseline 保存+对比")


def test_suite_selection() -> None:
    assert len(tasks.suite("smoke")) == 3
    assert len(tasks.suite("full")) >= 7
    assert {t.id for t in tasks.by_ids(["launch_calc", "git_clone"])} == {"launch_calc", "git_clone"}
    # every task has a callable check + setup/cleanup
    for t in tasks.SUITE:
        assert callable(t.check) and callable(t.setup) and callable(t.cleanup)
    print("[OK] suite 选择 + 任务结构完整")


if __name__ == "__main__":
    test_runner_with_stub()
    test_suite_selection()
    print("\neval runner smoke 全部通过 ✓")
