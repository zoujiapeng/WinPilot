"""Trace recording, script export, and offline (0-token) replay.

- TraceRecorder: a BUS sink that writes every event of the active run to
  ``traces/<run_id>.jsonl`` and saves a screenshot after each action tool.
- export_script: distill a trace into a ``.wps.json`` replay script whose
  steps carry multi-level selectors (element text → recorded coords).
- replay: execute a script offline, re-locating elements on the live screen
  through the same perception fusion, with the same verify engine.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import cv2

from ..action import executor, verify
from ..config import SCRIPTS_DIR, TRACES_DIR
from ..perception import fusion, win32
from ..utils.log import BUS, logger
from ..utils.screenshot import capture

_SCREENSHOT_TOOLS = {"click", "type_text", "hotkey", "launch", "drag", "scroll"}
_SCREENSHOT_MAX_W = 960


class TraceRecorder:
    """Singleton sink: call start(run_id) before a run, stop() after."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id: str | None = None
        self._file = None
        self._step = 0
        BUS.add_sink(self._on_event)

    def start(self, run_id: str) -> None:
        with self._lock:
            self._close()
            TRACES_DIR.mkdir(exist_ok=True)
            (TRACES_DIR / run_id).mkdir(exist_ok=True)
            self._file = open(TRACES_DIR / f"{run_id}.jsonl", "a", encoding="utf-8")
            self._run_id = run_id
            self._step = 0
        BUS.publish("trace", state="recording", run_id=run_id)

    def stop(self) -> None:
        with self._lock:
            run_id = self._run_id
            self._close()
        if run_id:
            BUS.publish("trace", state="stopped", run_id=run_id)

    def _close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        self._run_id = None

    def _on_event(self, event: dict[str, Any]) -> None:
        # Phase 1 (locked): claim a step number; never do I/O while locked.
        with self._lock:
            if self._file is None or event.get("type") == "trace":
                return
            run_id = self._run_id
            step = None
            if event.get("type") == "agent_tool" and event.get("tool") in _SCREENSHOT_TOOLS:
                self._step += 1
                step = self._step
        # Phase 2 (unlocked): screen grab + JPEG encode are slow.
        record = dict(event)
        if step is not None:
            shot = self._save_screenshot(run_id, step)
            if shot:
                record["screenshot"] = shot
        # Phase 3 (locked): append, re-checking the file is still open.
        with self._lock:
            if self._file is None or self._run_id != run_id:
                return
            try:
                self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self._file.flush()
            except OSError as exc:
                logger.warning("trace 写入失败: %s", exc)

    def _save_screenshot(self, run_id: str | None, step: int) -> str | None:
        if not run_id:
            return None
        try:
            image = capture()
            h, w = image.shape[:2]
            if w > _SCREENSHOT_MAX_W:
                scale = _SCREENSHOT_MAX_W / w
                image = cv2.resize(image, (_SCREENSHOT_MAX_W, int(h * scale)))
            rel = f"{run_id}/step_{step:03d}.jpg"
            path = TRACES_DIR / rel
            ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                path.write_bytes(buf.tobytes())
                return rel
        except Exception as exc:
            logger.debug("trace 截图失败: %s", exc)
        return None


RECORDER = TraceRecorder()


# ------------------------------------------------------------------ listing
def list_traces() -> list[dict[str, Any]]:
    """Trace runs on disk, newest first, with task + status summary."""
    TRACES_DIR.mkdir(exist_ok=True)
    out: list[dict[str, Any]] = []
    for path in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        info: dict[str, Any] = {"run_id": path.stem, "task": "", "status": "",
                                "steps": 0, "size_kb": round(path.stat().st_size / 1024, 1)}
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    event = json.loads(line)
                    if event.get("type") == "agent_start":
                        info["task"] = event.get("task", "")
                    elif event.get("type") == "agent_tool":
                        info["steps"] += 1
                    elif event.get("type") == "agent_done":
                        info["status"] = event.get("status", "")
        except (json.JSONDecodeError, OSError):
            pass
        out.append(info)
    return out


def read_trace(run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    path = TRACES_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(events) >= limit:
                break
    return events


# ------------------------------------------------------------------- export
def export_script(run_id: str, name: str | None = None) -> Path:
    """Distill a trace into a replayable .wps.json script."""
    events = read_trace(run_id)
    if not events:
        raise FileNotFoundError(f"trace 不存在: {run_id}")
    task = next(
        (e.get("task", "") for e in events if e.get("type") == "agent_start"), "")
    steps = [
        e["replay"] for e in events
        if e.get("type") == "agent_tool" and e.get("replay")
    ]
    if not steps:
        raise ValueError("trace 中没有可重放的动作步骤")
    script = {
        "format": "wps/1",
        "name": name or task[:60] or run_id,
        "task": task,
        "source_run": run_id,
        "created": int(time.time()),
        "steps": steps,
    }
    SCRIPTS_DIR.mkdir(exist_ok=True)
    path = SCRIPTS_DIR / f"{name or run_id}.wps.json"
    path.write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    BUS.publish("trace", state="exported", run_id=run_id, script=str(path))
    return path


def list_scripts() -> list[dict[str, Any]]:
    SCRIPTS_DIR.mkdir(exist_ok=True)
    out = []
    for path in sorted(SCRIPTS_DIR.glob("*.wps.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "file": path.name, "name": data.get("name", path.stem),
                "task": data.get("task", ""), "steps": len(data.get("steps", [])),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return out


# ------------------------------------------------------------------- replay
class Replayer:
    """Offline script execution: selector-based re-location + verify."""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stop_event = stop_event or threading.Event()
        self.hwnd: int | None = None

    def _relocate(self, step: dict) -> tuple[int, int] | None:
        """Find click target on the live screen: text match → recorded coords."""
        text = (step.get("text") or step.get("element_text") or "").strip()
        if text and self.hwnd:
            try:
                snapshot = fusion.observe(self.hwnd, include_icons=False)
                exact = [e for e in snapshot.elements if e.text.strip() == text]
                partial = [e for e in snapshot.elements if text in e.text]
                hit = (exact or partial or [None])[0]
                if hit:
                    return hit.cx, hit.cy
            except Exception as exc:
                logger.debug("replay 重定位失败: %s", exc)
        coords = step.get("coords")
        return (int(coords[0]), int(coords[1])) if coords else None

    def _run_step(self, idx: int, step: dict) -> dict[str, Any]:
        tool = step.get("tool", "")
        expect = step.get("expect")

        if tool == "launch":
            return verify.run_with_verify(
                "launch", lambda: executor.launch(step["command"]),
                expect=expect, hwnd=self.hwnd, timeout_s=10.0)
        if tool == "focus_window":
            found = win32.find_window(step.get("title", ""))
            if not found:
                return {"ok": False, "error": f'窗口 "{step.get("title")}" 不存在'}
            self.hwnd = found["hwnd"]
            return executor.focus_window(self.hwnd)
        if tool == "click":
            point = self._relocate(step)
            if point is None:
                return {"ok": False, "error": "无法定位点击目标"}
            x, y = point
            return verify.run_with_verify(
                "click",
                lambda: executor.click(x, y, button=step.get("button", "left"),
                                       double=bool(step.get("double")), hwnd=self.hwnd),
                expect=expect, hwnd=self.hwnd)
        if tool == "type_text":
            point = self._relocate(step)

            def action() -> dict:
                if point:
                    executor.click(point[0], point[1], hwnd=self.hwnd)
                    time.sleep(0.15)
                return executor.type_text(step.get("text_input", ""))

            return verify.run_with_verify(
                "type_text", action, expect=expect, hwnd=self.hwnd, retryable=False)
        if tool == "hotkey":
            return verify.run_with_verify(
                "hotkey", lambda: executor.press_keys(step.get("keys", [])),
                expect=expect, hwnd=self.hwnd, retryable=False)
        if tool == "scroll":
            coords = step.get("coords", [0, 0])
            return executor.scroll(coords[0], coords[1], int(step.get("amount", 0)))
        if tool == "drag":
            frm, to = step.get("from", [0, 0]), step.get("to", [0, 0])
            return executor.drag(frm[0], frm[1], to[0], to[1])
        if tool == "run_shell":
            from ..action import shell
            return shell.run(step.get("command", ""),
                             shell=step.get("shell", "powershell"),
                             timeout_s=float(step.get("timeout_s", 30)))
        if tool == "tray_click":
            from ..perception import tray
            return tray.click_tray_icon(
                step.get("name", ""), double=bool(step.get("double")),
                button=step.get("button", "left"))
        if tool == "wait":
            outcome = verify.check(step.get("condition", {}), hwnd=self.hwnd,
                                   timeout_s=float(step.get("timeout_s", 10)))
            return {"ok": outcome.verified, **outcome.to_dict()}
        return {"ok": False, "error": f"未知步骤类型: {tool}"}

    def run(self, script_path: str | Path) -> dict[str, Any]:
        path = Path(script_path)
        if not path.is_absolute():
            path = SCRIPTS_DIR / path
        script = json.loads(path.read_text(encoding="utf-8"))
        steps = script.get("steps", [])
        BUS.publish("replay", state="start", script=path.name, total=len(steps))
        results = []
        ok_all = True
        for idx, step in enumerate(steps, start=1):
            if self.stop_event.is_set():
                BUS.publish("replay", state="stopped", at=idx)
                return {"ok": False, "stopped_at": idx, "results": results}
            result = self._run_step(idx, step)
            step_ok = bool(result.get("ok", True)) and result.get("verified", True) is not False
            BUS.publish("replay", state="step", index=idx, tool=step.get("tool"),
                        ok=step_ok, detail={k: v for k, v in result.items()
                                            if k in ("error", "verify_detail", "attempts")})
            results.append({"index": idx, "tool": step.get("tool"), "ok": step_ok})
            if not step_ok:
                ok_all = False
                break
            time.sleep(0.25)
        BUS.publish("replay", state="done", ok=ok_all)
        return {"ok": ok_all, "results": results}


def replay(script_path: str | Path, stop_event: threading.Event | None = None) -> dict:
    return Replayer(stop_event).run(script_path)
