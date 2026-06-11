"""FastAPI server: REST control surface + WebSocket event stream + static UI.

One agent run at a time. All BUS events stream to every connected browser.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent.loop import AgentRunner
from .config import CONFIG, ROOT_DIR, SCRIPTS_DIR, TRACES_DIR
from .perception import ocr, vlm
from .recorder import trace as trace_mod
from .utils.log import BUS, logger

WEBUI_DIR = ROOT_DIR / "webui"

app = FastAPI(title="WinPilot", docs_url=None, redoc_url=None)

_origin = (
    f'http://{CONFIG.get("server", "host", default="127.0.0.1")}'
    f':{CONFIG.get("server", "port", default=8765)}'
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_origin],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


def _ensure_inside(path: Path, root: Path, what: str) -> Path:
    """Resolve and reject anything that escapes the expected directory."""
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(404, f"{what}不存在") from None
    return resolved


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ----------------------------------------------------------------- run state
class _RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runner: AgentRunner | None = None
        self.thread: threading.Thread | None = None
        self.replay_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def busy(self) -> bool:
        running_agent = self.thread is not None and self.thread.is_alive()
        running_replay = self.replay_thread is not None and self.replay_thread.is_alive()
        return running_agent or running_replay


STATE = _RunState()


# -------------------------------------------------------------------- models
class ChatRequest(BaseModel):
    task: str


class ReplayRequest(BaseModel):
    script: str


class ExportRequest(BaseModel):
    name: str | None = None


# ----------------------------------------------------------------- endpoints
@app.post("/api/chat")
def start_chat(req: ChatRequest) -> dict[str, Any]:
    task = req.task.strip()
    if not task:
        raise HTTPException(400, "任务不能为空")
    with STATE.lock:
        if STATE.busy():
            raise HTTPException(409, "已有任务在运行，请先停止")
        STATE.stop_event = threading.Event()
        runner = AgentRunner(task, stop_event=STATE.stop_event)
        STATE.runner = runner
        trace_mod.RECORDER.start(runner.run_id)
        BUS.publish("chat", run_id=runner.run_id, role="user", content=task)

        def worker() -> None:
            try:
                runner.run()
            finally:
                trace_mod.RECORDER.stop()

        STATE.thread = threading.Thread(target=worker, daemon=True, name="agent-run")
        STATE.thread.start()
    return {"run_id": runner.run_id}


@app.post("/api/stop")
def stop_run() -> dict[str, Any]:
    STATE.stop_event.set()
    return {"ok": True}


@app.post("/api/interject")
def interject(req: ChatRequest) -> dict[str, Any]:
    """Inject a hint into the running agent (steer it mid-task)."""
    text = req.task.strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    runner = STATE.runner
    agent_alive = STATE.thread is not None and STATE.thread.is_alive()
    if not runner or not agent_alive:
        raise HTTPException(409, "当前没有运行中的任务")
    runner.interject(text)
    return {"ok": True}


@app.get("/api/status")
def status() -> dict[str, Any]:
    runner = STATE.runner
    return {
        "running": STATE.busy(),
        "run_id": runner.run_id if runner else None,
        "run_status": runner.status if runner else None,
        "usage": runner.usage_total if runner else None,
        "ocr_provider": ocr.active_provider(),
        "vlm_configured": vlm.is_configured(),
        "enabled_dimensions": CONFIG.enabled_dimensions(),
        "is_admin": _is_admin(),
        "shell_enabled": bool(CONFIG.get("agent", "shell_enabled", default=True)),
    }


@app.post("/api/elevate")
def elevate() -> dict[str, Any]:
    """Relaunch WinPilot elevated (one UAC prompt), then exit this instance."""
    if _is_admin():
        return {"ok": True, "already_admin": True}
    run_py = str(ROOT_DIR / "run.py")
    try:
        # ShellExecuteW "runas" triggers the UAC consent dialog.
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{run_py}"', str(ROOT_DIR), 1)
        if rc <= 32:
            return {"ok": False, "error": f"提权失败（用户可能取消了 UAC），代码 {rc}"}
    except Exception as exc:
        raise HTTPException(500, f"提权失败: {exc}") from exc
    BUS.publish("config", changed=["elevate"], note="正在以管理员身份重启…")
    # Give the new elevated process time to bind the port, then exit ours.
    threading.Timer(1.5, lambda: __import__("os")._exit(0)).start()
    return {"ok": True, "restarting": True}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    snapshot = CONFIG.snapshot()
    key = snapshot.get("api", {}).get("api_key", "")
    if key:
        snapshot["api"]["api_key"] = "***" + key[-4:]
    vkey = snapshot.get("vlm", {}).get("api_key", "")
    if vkey:
        snapshot["vlm"]["api_key"] = "***" + vkey[-4:]
    return snapshot


@app.patch("/api/config")
def patch_config(patch: dict[str, Any]) -> dict[str, Any]:
    # Don't let the masked placeholder overwrite the real key.
    for section in ("api", "vlm"):
        sec = patch.get(section)
        if isinstance(sec, dict) and str(sec.get("api_key", "")).startswith("***"):
            sec.pop("api_key")
    CONFIG.update(patch)
    BUS.publish("config", changed=list(patch.keys()))
    return get_config()


@app.get("/api/traces")
def traces() -> list[dict[str, Any]]:
    return trace_mod.list_traces()


@app.get("/api/trace/{run_id}")
def trace_detail(run_id: str) -> list[dict[str, Any]]:
    events = trace_mod.read_trace(run_id)
    if not events:
        raise HTTPException(404, "trace 不存在")
    return events


@app.get("/api/trace/{run_id}/shot/{name}")
def trace_screenshot(run_id: str, name: str):
    path = _ensure_inside(TRACES_DIR / run_id / name, TRACES_DIR, "截图")
    if not path.exists():
        raise HTTPException(404, "截图不存在")
    return FileResponse(path)


@app.post("/api/export/{run_id}")
def export(run_id: str, req: ExportRequest | None = None) -> dict[str, Any]:
    try:
        path = trace_mod.export_script(run_id, name=(req.name if req else None))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "script": path.name}


@app.get("/api/scripts")
def scripts() -> list[dict[str, Any]]:
    return trace_mod.list_scripts()


@app.post("/api/replay")
def replay(req: ReplayRequest) -> dict[str, Any]:
    script_path = _ensure_inside(SCRIPTS_DIR / req.script, SCRIPTS_DIR, "脚本")
    if not script_path.exists():
        raise HTTPException(404, "脚本不存在")
    with STATE.lock:
        if STATE.busy():
            raise HTTPException(409, "已有任务在运行")
        STATE.stop_event = threading.Event()
        stop_event = STATE.stop_event

        def worker() -> None:
            try:
                trace_mod.replay(script_path, stop_event=stop_event)
            except Exception as exc:
                logger.exception("重放失败")
                BUS.publish("replay", state="error", error=str(exc))

        STATE.replay_thread = threading.Thread(target=worker, daemon=True, name="replay")
        STATE.replay_thread.start()
    return {"ok": True}


@app.get("/api/memory")
def memory_list() -> list[dict[str, Any]]:
    from .memory import experience
    return experience.all_recipes()


@app.delete("/api/memory")
def memory_clear() -> dict[str, Any]:
    from .memory import experience
    experience.clear()
    return {"ok": True}


# ---------------------------------------------------------------- websocket
@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    q = BUS.subscribe()
    try:
        while True:
            try:
                event = await asyncio.to_thread(q.get, True, 1.0)
            except queue.Empty:
                # heartbeat doubles as disconnect detection
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        BUS.unsubscribe(q)


# ------------------------------------------------------------------- static
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBUI_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")
