"""CLI: python -m winpilot.eval [smoke|full] [--tasks id,id] [--save-baseline]

Real API spend + GUI control — opt-in by running this manually.
"""
from __future__ import annotations

import sys

from . import runner, tasks


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    suite_name = args[0] if args else "smoke"

    if "--tasks" in argv:
        idx = argv.index("--tasks")
        ids = argv[idx + 1].split(",") if idx + 1 < len(argv) else []
        chosen = tasks.by_ids(ids)
    else:
        chosen = tasks.suite(suite_name)

    if not chosen:
        print("没有匹配的任务。可用:", ", ".join(t.id for t in tasks.SUITE))
        return 1

    runner.run(chosen, save_baseline=("--save-baseline" in flags))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
