"""Action→Verify→Retry engine.

Every action tool can carry an ``expect`` condition (a small DSL dict). After
executing, we poll the condition until verified or timeout; failed checks
escalate through perception dimensions (fused observe → window OCR →
full-screen OCR) and failed actions are re-executed up to ``max_retries``
times when safe. Results always report ``verified`` + a ``post_state``
summary so the model can self-correct.

Expect DSL (keys may be combined; all must pass):
    {"text_appears": "另存为"}
    {"text_gone": "正在加载"}
    {"window_appears": "另存为"}
    {"window_gone": "记事本"}
    {"screen_stable": true}                          # or {"stable_ms":600,"timeout_s":10}
    {"color_match": {"x":10,"y":20,"rgb":[0,120,215],"tolerance":40}}
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import win32gui

from ..config import CONFIG
from ..perception import fusion, groundtruth, ocr, vision_cv, win32
from ..utils.log import BUS, logger
from ..utils.screenshot import Region, screen_size, window_region

_POLL_INTERVAL_S = 0.35
_DEFAULT_TIMEOUT_S = 6.0
_COLOR_DEFAULT_TOLERANCE = 40

SUPPORTED_CONDITIONS = (
    "text_appears", "text_gone", "window_appears", "window_gone",
    "screen_stable", "color_match",
) + groundtruth.GROUNDTRUTH_KEYS


@dataclass
class VerifyOutcome:
    """Result of evaluating one expect dict."""

    verified: bool
    detail: str
    method: str = ""           # which dimension confirmed/refuted (uia/ocr/win32/cv)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "detail": self.detail,
            "method": self.method,
            "elapsed_ms": self.elapsed_ms,
        }


# ------------------------------------------------------------ target window
def _target_hwnd(hwnd: int | None) -> int | None:
    """Prefer the foreground window (dialogs are separate top-levels)."""
    fg = win32gui.GetForegroundWindow()
    if fg and win32gui.IsWindowVisible(fg):
        return fg
    return hwnd


def _full_screen_region() -> Region:
    sw, sh = screen_size()
    return Region(0, 0, sw, sh)


# --------------------------------------------------------- condition checks
def _find_text(needle: str, hwnd: int | None, level: int) -> tuple[bool, str]:
    """Search for text with escalating perception. Returns (found, method)."""
    needle_lower = needle.lower()
    target = _target_hwnd(hwnd)

    # Level 0: fused observe with whatever dimensions are enabled (fast path).
    if target:
        try:
            snapshot = fusion.observe(target, include_icons=False)
            for element in snapshot.elements:
                if needle_lower in element.text.lower():
                    return True, element.source
        except Exception as exc:
            logger.debug("verify: fused observe 失败: %s", exc)

    if level < 1 or not _ocr_enabled():
        return False, ""

    # Level 1: direct OCR over the target window (fresh capture).
    if target:
        try:
            if ocr.find_text(window_region(target), needle):
                return True, "ocr"
        except Exception as exc:
            logger.debug("verify: 窗口 OCR 失败: %s", exc)

    if level < 2:
        return False, ""

    # Level 2: full-screen OCR — catches toasts/dialogs outside the window.
    # Last resort, so trade precision for recall with a lowered threshold.
    try:
        if ocr.find_text(_full_screen_region(), needle, min_conf=0.4):
            return True, "ocr-fullscreen"
    except Exception as exc:
        logger.debug("verify: 全屏 OCR 失败: %s", exc)
    return False, ""


def _ocr_enabled() -> bool:
    return bool(CONFIG.get("perception", "ocr", "enabled", default=False))


def _check_window(needle: str, want_present: bool) -> tuple[bool, str]:
    found = win32.find_window(needle)
    if want_present:
        return (found is not None), (found["title"] if found else "")
    return (found is None), (found["title"] if found else "")


def _check_color(spec: dict, _hwnd: int | None) -> tuple[bool, str]:
    x, y = int(spec["x"]), int(spec["y"])
    expected = spec.get("rgb") or spec.get("color")
    tolerance = int(spec.get("tolerance", _COLOR_DEFAULT_TOLERANCE))
    probe = vision_cv.color_probe(x, y)
    actual = probe["average_rgb"]
    if expected is None:
        # An expectation without a target colour is a malformed condition —
        # failing loudly tells the model to fix it (use color_probe to probe).
        return False, f"color_match 缺少 rgb 期望值（实际颜色 {probe['average_hex']}）"
    distance = max(abs(int(a) - int(b)) for a, b in zip(actual, expected))
    ok = distance <= tolerance
    return ok, f"期望RGB{tuple(expected)} 实际{actual} 最大偏差{distance}"


def _check_screen_stable(spec: Any, hwnd: int | None,
                         deadline: float | None = None) -> tuple[bool, str]:
    opts = spec if isinstance(spec, dict) else {}
    target = _target_hwnd(hwnd)
    region = window_region(target) if target else _full_screen_region()
    timeout = float(opts.get("timeout_s", 10.0))
    if deadline is not None:  # never overrun the outer verify budget
        timeout = max(0.5, min(timeout, deadline - time.monotonic()))
    result = vision_cv.wait_screen_stable(
        region,
        stable_ms=int(opts.get("stable_ms", 600)),
        timeout_s=timeout,
    )
    return result["stable"], f"等待{result['waited_s']}s"


# ----------------------------------------------------------------- evaluate
def _eval_once(expect: dict, hwnd: int | None, level: int,
               deadline: float | None = None) -> VerifyOutcome:
    """Evaluate every condition in the expect dict once (AND semantics)."""
    for key, value in expect.items():
        if key == "text_appears":
            ok, method = _find_text(str(value), hwnd, level)
            if not ok:
                return VerifyOutcome(False, f'未找到文字 "{value}"', method)
        elif key == "text_gone":
            ok, method = _find_text(str(value), hwnd, max(level, 1))
            if ok:
                return VerifyOutcome(False, f'文字 "{value}" 仍存在', method)
        elif key == "window_appears":
            ok, title = _check_window(str(value), want_present=True)
            if not ok:
                return VerifyOutcome(False, f'窗口 "{value}" 未出现', "win32")
        elif key == "window_gone":
            ok, title = _check_window(str(value), want_present=False)
            if not ok:
                return VerifyOutcome(False, f'窗口 "{title}" 仍存在', "win32")
        elif key == "screen_stable":
            ok, detail = _check_screen_stable(value, hwnd, deadline=deadline)
            if not ok:
                return VerifyOutcome(False, f"画面未稳定 ({detail})", "cv")
        elif key == "color_match":
            ok, detail = _check_color(value, hwnd)
            if not ok:
                return VerifyOutcome(False, f"颜色不符 ({detail})", "cv")
        elif key in groundtruth.GROUNDTRUTH_KEYS:
            # Deterministic ground truth (filesystem/process/shell) — the
            # strongest signal; no perception escalation needed. Bound any
            # shell_true to the remaining verify budget so it can't overrun.
            remaining = max(1.0, deadline - time.monotonic()) if deadline else 15.0
            ok, detail = groundtruth.check(key, value, timeout_s=remaining)
            if not ok:
                return VerifyOutcome(False, f"{key} 未满足 ({detail})", "groundtruth")
        else:
            return VerifyOutcome(False, f"不支持的验证条件: {key}", "")
    return VerifyOutcome(True, "全部条件满足", "")


def check(expect: dict, hwnd: int | None = None,
          timeout_s: float = _DEFAULT_TIMEOUT_S) -> VerifyOutcome:
    """Poll the expect conditions until verified or timeout, escalating
    perception level (0=fused → 1=window OCR → 2=full-screen OCR) over time."""
    started = time.monotonic()
    deadline = started + timeout_s
    level = 0
    last = VerifyOutcome(False, "未开始", "")
    while True:
        last = _eval_once(expect, hwnd, level, deadline=deadline)
        elapsed = time.monotonic() - started
        last.elapsed_ms = int(elapsed * 1000)
        if last.verified or time.monotonic() >= deadline:
            if last.verified and not last.method:
                last.method = "fused"
            return last
        # escalate: >1/3 of budget → window OCR; >2/3 → full-screen OCR
        if elapsed > timeout_s * 2 / 3:
            level = 2
        elif elapsed > timeout_s / 3:
            level = 1
        time.sleep(_POLL_INTERVAL_S)


# --------------------------------------------------------------- post state
def post_state_summary() -> dict[str, Any]:
    """Cheap snapshot of "where are we now" for the model after each action."""
    fg = win32gui.GetForegroundWindow()
    title = win32._window_text(fg) if fg else ""
    return {"foreground_hwnd": fg, "foreground_title": title}


# ------------------------------------------------------------ run + verify
def run_with_verify(
    name: str,
    action: Callable[[], dict],
    expect: dict | None = None,
    hwnd: int | None = None,
    retryable: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Execute an action, verify the expectation, retry on failure.

    Non-retryable actions (e.g. type_text — re-running duplicates input) are
    executed once; only verification is repeated.
    """
    max_retries = int(CONFIG.get("agent", "max_retries", default=2))
    attempts = 0
    result: dict[str, Any] = {}
    outcome: VerifyOutcome | None = None
    # Hard wall-clock cap so a single action can never hang for minutes
    # (e.g. screen_stable on a never-settling canvas + slow OCR escalation
    # × retries). Once exceeded, stop retrying and report the last outcome.
    overall_deadline = time.monotonic() + max(timeout_s * (max_retries + 1), timeout_s) + 4.0

    while True:
        attempts += 1
        try:
            result = action() or {}
        except Exception as exc:
            logger.warning("动作 %s 执行异常: %s", name, exc)
            result = {"ok": False, "error": str(exc)}
            # Don't verify a screen the action never touched — a stale match
            # would falsely report success. Retry the action or give up.
            if retryable and attempts <= max_retries:
                continue
            outcome = VerifyOutcome(False, f"动作执行异常: {exc}", "") if expect else None
            break

        if not expect:
            outcome = None
            break

        # Shrink the verify budget to whatever's left of the overall cap.
        budget = max(1.0, min(timeout_s, overall_deadline - time.monotonic()))
        outcome = check(expect, hwnd=hwnd, timeout_s=budget)
        BUS.publish(
            "verify",
            action=name,
            attempt=attempts,
            verified=outcome.verified,
            detail=outcome.detail,
            method=outcome.method,
            elapsed_ms=outcome.elapsed_ms,
        )
        if outcome.verified:
            break
        if (not retryable or attempts > max_retries or not result.get("ok", True)
                or time.monotonic() >= overall_deadline):
            break
        logger.info("动作 %s 验证失败（第%d次），重试: %s", name, attempts, outcome.detail)

    response = dict(result)
    response["attempts"] = attempts
    if outcome is not None:
        response["verified"] = outcome.verified
        response["verify_detail"] = outcome.detail
        response["verify_method"] = outcome.method
    response["post_state"] = post_state_summary()
    return response
