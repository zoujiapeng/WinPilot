"""Eval runner: run tasks against the live in-process agent, score with
programmatic ground-truth checks, emit a scorecard + baseline comparison.

This is the standard against which every future change is measured: success
rate, avg steps, avg cost. "Did this change help or regress?" becomes a number.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..agent.loop import AgentRunner
from ..config import CONFIG
from ..recorder import trace as trace_mod
from .tasks import EvalTask

RESULTS_DIR = Path(__file__).resolve().parent / "results"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def _run_once(task: EvalTask) -> dict[str, Any]:
    """One agent run of a task's prompt; returns metrics (no check yet)."""
    runner = AgentRunner(task.prompt, session_context=[])
    trace_mod.RECORDER.start(runner.run_id)
    started = time.monotonic()
    try:
        runner.run()
    finally:
        trace_mod.RECORDER.stop()
    return {
        "run_id": runner.run_id,
        "status": runner.status,
        "result_text": runner.result_text,
        "steps": len(runner._steps_log),
        "usage": dict(runner.usage_total),
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def run_task(task: EvalTask) -> dict[str, Any]:
    # Per-task max_steps override via the thread-safe public API (auto-restored).
    prev_steps = CONFIG.get("agent", "max_steps", default=40)
    CONFIG.set(task.max_steps, "agent", "max_steps")
    try:
        try:
            task.setup()
        except Exception as exc:  # a bad setup must not crash the whole suite
            return {"id": task.id, "tags": list(task.tags), "pass": False,
                    "detail": f"setup 异常: {exc}", "agent_status": "skipped",
                    "steps": 0, "cost_cny": 0.0, "elapsed_s": 0.0, "run_ids": []}
        runs = [_run_once(task) for _ in range(max(1, task.repeat))]
    finally:
        CONFIG.set(prev_steps, "agent", "max_steps")

    last = runs[-1]
    ctx = {
        "result_text": last["result_text"],
        "run_id": last["run_id"],
        "run_ids": [r["run_id"] for r in runs],
        "status": last["status"],
        "steps": last["steps"],
        "usage": last["usage"],
    }
    try:
        ok, detail = task.check(ctx)
    except Exception as exc:  # a flaky checker must not crash the suite
        ok, detail = False, f"检查器异常: {exc}"
    finally:
        try:
            task.cleanup()
        except Exception:
            pass

    cost = sum(r["usage"].get("cost_cny", 0) for r in runs)
    return {
        "id": task.id,
        "tags": list(task.tags),
        "pass": bool(ok),                 # ground-truth success (the real metric)
        "detail": detail,
        "agent_status": last["status"],   # what the agent claimed
        "steps": last["steps"],
        "cost_cny": round(cost, 4),
        "elapsed_s": sum(r["elapsed_s"] for r in runs),
        "run_ids": ctx["run_ids"],
    }


def _scorecard(results: list[dict]) -> dict[str, Any]:
    n = len(results)
    passed = sum(r["pass"] for r in results)
    return {
        "total": n,
        "passed": passed,
        "success_rate": round(passed / n, 3) if n else 0.0,
        "avg_steps": round(sum(r["steps"] for r in results) / n, 1) if n else 0,
        "total_cost_cny": round(sum(r["cost_cny"] for r in results), 4),
        "total_elapsed_s": round(sum(r["elapsed_s"] for r in results), 1),
        "results": results,
    }


def _fmt_table(results: list[dict]) -> str:
    lines = [f"{'task':<16}{'pass':<6}{'agent':<8}{'steps':<7}{'¥':<9}{'detail'}"]
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        lines.append(
            f'{r["id"]:<16}{mark:<6}{r["agent_status"]:<8}{r["steps"]:<7}'
            f'{r["cost_cny"]:<9}{r["detail"][:48]}')
    return "\n".join(lines)


def _signature(tasks: list[EvalTask]) -> str:
    return ",".join(sorted(t.id for t in tasks))


def _load_baselines() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _compare_baseline(card: dict, signature: str) -> str:
    base = _load_baselines().get(signature)
    if not base:
        return "（该任务集无 baseline，--save-baseline 可保存当前为基线）"
    d_rate = card["success_rate"] - base.get("success_rate", 0)
    d_steps = card["avg_steps"] - base.get("avg_steps", 0)
    d_cost = card["total_cost_cny"] - base.get("total_cost_cny", 0)
    arrow = "→" if abs(d_rate) < 1e-9 else ("↑" if d_rate > 0 else "↓")
    return (f"vs baseline: 成功率 {base.get('success_rate',0):.3f}→{card['success_rate']:.3f} "
            f"({d_rate:+.3f}{arrow}) | 均步 {d_steps:+.1f} | 花费 ¥{d_cost:+.4f}")


def run(suite_tasks: list[EvalTask], save_baseline: bool = False) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== WinPilot eval: {len(suite_tasks)} 个任务 (真API真GUI, 会花钱) ===")
    results = []
    for i, task in enumerate(suite_tasks, 1):
        print(f"\n[{i}/{len(suite_tasks)}] {task.id}: {task.prompt[:40]}…")
        res = run_task(task)
        print(f"  → {'PASS' if res['pass'] else 'FAIL'} | agent={res['agent_status']} "
              f"steps={res['steps']} ¥{res['cost_cny']} | {res['detail'][:60]}")
        results.append(res)

    card = _scorecard(results)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{stamp}.json"
    out.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print(_fmt_table(results))
    print("-" * 70)
    print(f"成功率 {card['passed']}/{card['total']} = {card['success_rate']:.0%} | "
          f"均步 {card['avg_steps']} | 总花费 ¥{card['total_cost_cny']} | "
          f"耗时 {card['total_elapsed_s']}s")
    print(_compare_baseline(card, _signature(suite_tasks)))
    print(f"计分卡: {out}")

    if save_baseline:
        baselines = _load_baselines()
        baselines[_signature(suite_tasks)] = card
        BASELINE_PATH.write_text(json.dumps(baselines, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"✅ 已保存为该任务集的 baseline: {BASELINE_PATH}")
    return card
