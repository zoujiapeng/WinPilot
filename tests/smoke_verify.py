"""Smoke test for the verify engine (run from project root)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winpilot.action import verify  # noqa: E402
from winpilot.perception import win32  # noqa: E402

windows = win32.list_windows()
assert windows, "list_windows 返回空"
needle = windows[0]["title"][:10]
outcome = verify.check({"window_appears": needle}, timeout_s=2)
print(f"win_appears({needle!r}):", outcome.verified, outcome.detail, outcome.method)
assert outcome.verified

outcome = verify.check({"window_gone": "不存在的窗口XYZ123"}, timeout_s=1)
print("win_gone:", outcome.verified, outcome.detail)
assert outcome.verified

outcome = verify.check({"screen_stable": {"stable_ms": 300, "timeout_s": 4}}, timeout_s=5)
print("stable:", outcome.verified, outcome.detail)

outcome = verify.check({"text_appears": "绝不可能出现的文字串XQZ"}, timeout_s=2)
print("text_missing(expect False):", outcome.verified, outcome.detail)
assert not outcome.verified

print("post_state:", verify.post_state_summary())
print("SMOKE OK")
