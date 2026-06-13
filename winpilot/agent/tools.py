"""Agent tool definitions (OpenAI function-calling schemas) + dispatcher.

The dispatcher holds per-run session state: the latest Snapshot (element-id
registry) and the current target hwnd. Every action tool accepts an optional
``expect`` condition (see action/verify.py DSL) and runs through the
Action→Verify→Retry engine.
"""
from __future__ import annotations

import json
from typing import Any

import win32gui

from ..action import executor, shell, verify
from ..config import CONFIG
from ..memory import experience
from ..perception import apps, fusion, search, tray, vision_cv, vlm, win32, world
from ..utils.log import BUS, logger
from ..utils.screenshot import Region, window_region
from . import planner

_EXPECT_SCHEMA = {
    "type": "object",
    "description": (
        '验证条件,可组合: {"text_appears":"另存为"} {"text_gone":"..."} '
        '{"window_appears":"标题子串"} {"window_gone":"..."} {"screen_stable":true} '
        '{"color_match":{"x":1,"y":2,"rgb":[0,120,215],"tolerance":40}} '
        '【确定性ground-truth(最可靠,涉及文件/进程时优先用)】: '
        '{"file_exists":"路径"} {"file_contains":{"path":"..","text":".."}} '
        '{"process_running":"exe名"} {"process_gone":"exe名"} '
        '{"shell_true":"PowerShell表达式(真值即通过)"}'
    ),
}


def tool_schemas(planning: bool = False) -> list[dict]:
    """OpenAI tools array. ``planning`` adds the todo-management tools;
    vlm_describe only when a VLM is configured."""
    tools = [
        _tool("list_windows", "列出当前所有可见的顶层窗口（标题、hwnd、位置）", {}),
        _tool(
            "observe",
            "感知一个窗口：返回其全部 UI 元素列表（带元素id、文字、坐标、状态、来源维度）。"
            "不传 hwnd 则观察当前前台窗口。操作前必须先 observe。",
            {
                "hwnd": {"type": "integer", "description": "窗口句柄，省略=前台窗口"},
                "include_icons": {"type": "boolean", "description": "是否包含CV图标候选，默认true"},
            },
        ),
        _tool(
            "focus_window",
            "把窗口切到前台并设为当前操作目标",
            {"hwnd": {"type": "integer"}},
            required=["hwnd"],
        ),
        _tool(
            "find_app",
            "检查程序是否已在运行（即使窗口隐藏/在系统托盘）。返回进程数与所有窗口（含隐藏）。"
            "launch 任何可能已在后台的程序（QQ/微信等）前必须先调用此工具",
            {"name": {"type": "string", "description": "进程名子串，如 QQ、wechat、notepad"}},
            required=["name"],
        ),
        _tool(
            "find_program",
            "按显示名查找本机【已安装】的程序（支持中文名，如 网易云音乐、微信），"
            "返回可启动目标列表。找不到程序的 exe 名时必须用它，不要瞎猜",
            {"name": {"type": "string", "description": "程序显示名或其一部分"}},
            required=["name"],
        ),
        _tool(
            "tray_click",
            "点击系统托盘图标（自动展开隐藏图标溢出区）。用于唤起最小化到托盘的程序。"
            "找不到时会返回托盘现有图标名列表",
            {
                "name": {"type": "string", "description": "图标名子串，如 QQ"},
                "double": {"type": "boolean", "description": "双击，默认false"},
                "button": {"type": "string", "enum": ["left", "right"], "description": "默认left；right弹出托盘菜单"},
            },
            required=["name"],
        ),
        _tool(
            "launch",
            "启动程序（exe名/完整路径/shell命令/应用显示名如\"网易云音乐\"——"
            "非可执行命令会自动按已安装应用解析启动）",
            {"command": {"type": "string"}, "expect": _EXPECT_SCHEMA},
            required=["command"],
        ),
        _tool(
            "click",
            "点击。优先传 element_id（走UIA快路径）；或传裸坐标 x,y",
            {
                "element_id": {"type": "integer", "description": "最近一次observe中的元素id"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "double": {"type": "boolean"},
                "expect": _EXPECT_SCHEMA,
            },
        ),
        _tool(
            "type_text",
            "输入文字（支持中文，自动处理输入法）。可先传 element_id 点击聚焦该输入框",
            {
                "text": {"type": "string"},
                "element_id": {"type": "integer", "description": "可选，先聚焦的输入框元素id"},
                "expect": _EXPECT_SCHEMA,
            },
            required=["text"],
        ),
        _tool(
            "hotkey",
            '按快捷键组合，如 ["ctrl","s"]、["enter"]、["alt","f4"]',
            {
                "keys": {"type": "array", "items": {"type": "string"}},
                "expect": _EXPECT_SCHEMA,
            },
            required=["keys"],
        ),
        _tool(
            "scroll",
            "在坐标处滚动，amount 正=向上 负=向下（格数）",
            {
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "amount": {"type": "integer"},
            },
            required=["x", "y", "amount"],
        ),
        _tool(
            "drag",
            "从(x1,y1)拖拽到(x2,y2)",
            {
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
            },
            required=["x1", "y1", "x2", "y2"],
        ),
        _tool(
            "wait",
            "等待条件满足（不执行任何动作），如等窗口出现/画面稳定",
            {
                "condition": _EXPECT_SCHEMA,
                "timeout_s": {"type": "number", "description": "默认10秒"},
            },
            required=["condition"],
        ),
        _tool(
            "read_text",
            "读取窗口内全部可见文字（融合感知+OCR），用于读结果/确认内容",
            {"hwnd": {"type": "integer", "description": "省略=当前目标窗口"}},
        ),
        _tool(
            "find_image",
            "在屏幕上模板匹配查找图片（templates_img/下的模板名），返回坐标",
            {"template": {"type": "string"}},
            required=["template"],
        ),
        _tool(
            "capture_template",
            "把屏幕某区域截存为图像模板（之后可用 find_image 在屏幕上找它）。"
            "用于记住某个图标/按钮的样子，逐步积累图标库",
            {
                "left": {"type": "integer"}, "top": {"type": "integer"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
                "name": {"type": "string", "description": "模板名，如 chrome图标"},
            },
            required=["left", "top", "width", "height", "name"],
        ),
        _tool(
            "color_probe",
            "读取屏幕某点的颜色（平均色+主色，hex），判断按钮状态/主题/进度条",
            {"x": {"type": "integer"}, "y": {"type": "integer"}},
            required=["x", "y"],
        ),
        _tool(
            "undo_last",
            "撤销/回收本次最近的可逆动作:取消刚启动的后台/定时任务、关掉刚打开的程序窗口、"
            "或对当前窗口执行 Ctrl+Z。用户说'撤销/取消刚才/关掉它'时用。GUI 可逆性有限。",
            {},
        ),
        _tool(
            "search_files",
            "用 Everything 秒级搜索全盘文件名（找文件/歌曲/文档首选，远快于遍历）。"
            "返回匹配的完整路径列表。可选 path 限定目录",
            {
                "query": {"type": "string", "description": "文件名关键词或通配符，如 *.mp3、报告"},
                "max_results": {"type": "integer", "description": "默认50"},
                "path": {"type": "string", "description": "限定搜索目录，省略=全盘"},
            },
            required=["query"],
        ),
        _tool(
            "done",
            "任务成功完成时调用，结束运行",
            {"summary": {"type": "string", "description": "完成情况总结"}},
            required=["summary"],
        ),
        _tool(
            "fail",
            "确认任务无法完成时调用，结束运行",
            {"reason": {"type": "string"}},
            required=["reason"],
        ),
    ]
    if shell.shell_enabled():
        tools.insert(0, _tool(
            "run_detached",
            "启动一个【独立存活】的后台 PowerShell 脚本（不随本程序退出而死）。"
            "延时/定时/长时后台任务的唯一可靠方式——run_shell 的进程命令返回就死，"
            "Start-Job 也会随之死。脚本里用 Start-Sleep 做延时、用 msg.exe 或托盘气泡做非阻塞提醒"
            "（不要用会阻塞的 MessageBox）。返回 PID，安排后务必验证该进程存活。",
            {
                "script": {"type": "string",
                           "description": "完整 PowerShell 脚本（可含 Start-Sleep、Start-Process 等）"},
                "name": {"type": "string", "description": "任务名（用于脚本文件名），如 提醒吃饭"},
            },
            required=["script"],
        ))
        tools.insert(0, _tool(
            "run_shell",
            "执行 PowerShell/cmd 命令并返回输出（stdout/stderr/退出码）。"
            "文件增删改查移动、启动程序、播放媒体、系统/进程/服务查询等优先用它，比 GUI 快且稳。"
            "默认 powershell；命令可直接读到结果",
            {
                "command": {"type": "string", "description": "要执行的命令"},
                "shell": {"type": "string", "enum": ["powershell", "cmd"], "description": "默认powershell"},
                "timeout_s": {"type": "number", "description": "默认30秒"},
            },
            required=["command"],
        ))
    if planning:
        tools.append(_tool(
            "revise_plan",
            "把当前任务（重新）拆解为有序子目标 todo 列表。多步任务开始时或发现原计划不对时调用。",
            {"steps": {"type": "array", "items": {"type": "string"},
                       "description": "有序子目标。每项是一句简短动宾短语；"
                       "若某步有确定性完成信号,可写成对象 {\"goal\":\"..\",\"check\":{\"file_exists\":\"..\"}} 便于自动核验"}},
            required=["steps"],
        ))
        tools.append(_tool(
            "complete_subgoal",
            "标记某个子目标已完成（按计划里的序号）。完成一步就调用，便于跟踪进度。",
            {"index": {"type": "integer", "description": "计划中的子目标序号（从1开始）"}},
            required=["index"],
        ))
    if experience.enabled():
        tools.append(_tool(
            "remember",
            "把一条用户要求记住的信息/偏好/做法存入长期经验记忆（跨任务生效）。"
            "仅在用户明确要求记住某事时调用。",
            {
                "note": {"type": "string", "description": "要记住的内容，一句话"},
                "app": {"type": "string", "description": "关联的应用名，可选"},
            },
            required=["note"],
        ))
        tools.append(_tool(
            "forget",
            "按关键词删除匹配的经验记忆条目（用户要求忘掉某事时调用）。会返回删除了哪些。",
            {"query": {"type": "string", "description": "匹配任务/应用/步骤文本的关键词"}},
            required=["query"],
        ))
        tools.append(_tool(
            "reuse_skill",
            "复用上次某个成功任务的完整流程（0-token 离线重放，文字→坐标自动重定位）。"
            "当前任务与历史成功任务基本相同时优先用它，比一步步重做快得多。"
            "重放到某步失败会自动停下并告知失败位置，你再从那步起改用实时操作。",
            {
                "task": {"type": "string", "description": "要复用的历史任务描述（或其关键词）"},
                "recipe_id": {"type": "string", "description": "可选，直接指定经验条目 id"},
            },
        ))
    if vlm.is_configured():
        tools.append(
            _tool(
                "vlm_describe",
                "让视觉模型描述屏幕区域（看图片内容/图标含义）。不传区域=当前窗口",
                {
                    "question": {"type": "string"},
                    "left": {"type": "integer"}, "top": {"type": "integer"},
                    "width": {"type": "integer"}, "height": {"type": "integer"},
                },
                required=["question"],
            )
        )
    return tools


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


class ToolResult:
    """Dispatcher output: text for the LLM + metadata for the loop."""

    def __init__(self, text: str, is_observe: bool = False,
                 terminal: str | None = None, summary: str = "",
                 replay: dict | None = None) -> None:
        self.text = text
        self.is_observe = is_observe
        self.terminal = terminal          # None | "done" | "fail"
        self.summary = summary            # one-line replacement when folded
        self.replay = replay              # selector info for offline replay export


class ToolSession:
    """Per-run state: latest snapshot + current target window."""

    def __init__(self, stop_event=None) -> None:
        self.snapshot: fusion.Snapshot | None = None
        self.hwnd: int | None = None
        # Shared with the runner so long ops (reuse_skill replay) honor Stop.
        import threading
        self.stop_event = stop_event or threading.Event()
        # World model is fed from dispatch(); read once per run (runs are
        # short-lived, created per task) so a runtime toggle change applies
        # to the next task rather than mid-run.
        self.world_enabled: bool = bool(CONFIG.get("agent", "world_model", default=True))
        self.world = world.WorldModel()
        # Macro recovery: give the model one forced reconsideration before a
        # fail() actually terminates the run.
        self.replan_on_fail: bool = bool(CONFIG.get("agent", "replan_on_fail", default=True))
        self._fail_softened = False
        self.plan: planner.Plan | None = None
        # Lightweight undo stack: things this run created that can be rolled back.
        self._undo_stack: list[tuple[str, object]] = []

    # ----------------------------------------------------------- dispatch
    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolResult(f"未知工具: {name}")
        try:
            result = handler(args)
        except Exception as exc:
            logger.exception("工具 %s 执行异常", name)
            return ToolResult(f"工具 {name} 执行异常: {exc}")
        if self.world_enabled:
            try:
                self.world.record_action(name, args)
                if result.is_observe and self.snapshot is not None:
                    self.world.observe_snapshot(self.snapshot)
            except Exception:
                logger.exception("世界模型更新失败")  # never block tool flow
        return result

    def _resolve_hwnd(self, args: dict) -> int | None:
        hwnd = args.get("hwnd") or self.hwnd
        if not hwnd:
            fg = win32gui.GetForegroundWindow()
            hwnd = fg if fg else None
        return hwnd

    def _resolve_element(self, elem_id: int):
        if self.snapshot is None:
            return None, "尚未 observe，无元素可用"
        element = self.snapshot.by_id(elem_id)
        if element is None:
            return None, f"元素 id={elem_id} 不存在于最近一次 observe（请重新 observe）"
        return element, ""

    @staticmethod
    def _fmt(data: dict) -> str:
        return json.dumps(data, ensure_ascii=False, default=str)

    # -------------------------------------------------------------- tools
    def _t_list_windows(self, _args: dict) -> ToolResult:
        windows = win32.list_windows()
        lines = [
            f'hwnd={w["hwnd"]} "{w["title"]}" class={w["class_name"]} rect={w["rect"]}'
            for w in windows[:40]
        ]
        return ToolResult("\n".join(lines) or "无可见窗口",
                          summary=f"[已折叠] list_windows {len(windows)}个窗口")

    def _t_observe(self, args: dict) -> ToolResult:
        hwnd = self._resolve_hwnd(args)
        if not hwnd:
            return ToolResult("无法确定目标窗口，请先 list_windows + focus_window")
        include_icons = bool(args.get("include_icons", True))
        self.snapshot = fusion.observe(hwnd, include_icons=include_icons)
        self.hwnd = hwnd
        text = self.snapshot.to_text()
        summary = (
            f'[已折叠] observe "{self.snapshot.title}" '
            f"{len(self.snapshot.elements)}个元素"
        )
        return ToolResult(text, is_observe=True, summary=summary)

    def _t_focus_window(self, args: dict) -> ToolResult:
        hwnd = int(args["hwnd"])
        title = win32._window_text(hwnd)
        result = executor.focus_window(hwnd)
        if result.get("ok"):
            self.hwnd = hwnd
        return ToolResult(
            self._fmt(result),
            replay={"tool": "focus_window", "title": title},
        )

    def _t_find_app(self, args: dict) -> ToolResult:
        info = win32.find_app(str(args["name"]))
        return ToolResult(self._fmt(info))

    def _t_find_program(self, args: dict) -> ToolResult:
        candidates = apps.resolve(str(args["name"]))
        if not candidates:
            return ToolResult(
                f'未找到名称含 "{args["name"]}" 的已安装程序。'
                "可换更短的关键词重试，或用 search_files 搜可执行文件")
        lines = [
            f'{i}. "{c["name"]}" [{c["kind"]}] {c["target"]}'
            for i, c in enumerate(candidates, 1)
        ]
        return ToolResult(
            "已安装程序候选（直接 launch 其名称或完整 target 即可启动）:\n"
            + "\n".join(lines))

    def _t_run_shell(self, args: dict) -> ToolResult:
        command = str(args["command"])
        shell_kind = str(args.get("shell", "powershell"))
        timeout_s = float(args.get("timeout_s", 30))
        result = shell.run(command, shell=shell_kind, timeout_s=timeout_s)
        return ToolResult(
            self._fmt(result),
            replay={"tool": "run_shell", "command": command, "shell": shell_kind,
                    "timeout_s": timeout_s},
        )

    def _t_run_detached(self, args: dict) -> ToolResult:
        script = str(args["script"])
        name = str(args.get("name", "task"))
        result = shell.run_detached(script, name=name)
        if result.get("ok") and result.get("pid"):
            self._undo_stack.append(("detached", result["pid"]))  # cancelable
        return ToolResult(
            self._fmt(result),
            replay={"tool": "run_detached", "script": script, "name": name},
        )

    def _t_undo_last(self, _args: dict) -> ToolResult:
        """Best-effort rollback of the most recent reversible action this run:
        cancel a scheduled/background task, close the last launched app, or
        Ctrl+Z the foreground window. GUI is only partly reversible."""
        while self._undo_stack:
            kind, payload = self._undo_stack.pop()
            if kind == "detached":
                import subprocess
                subprocess.run(["taskkill", "/f", "/pid", str(payload)],
                               capture_output=True, timeout=10)
                BUS.publish("action", action="undo", what=f"取消后台任务 pid={payload}")
                return ToolResult(f"已取消最近的后台/定时任务 (pid={payload})。")
            if kind == "app":
                # Close the specific launched window (not the current foreground,
                # which the user may have switched). Skip if it's already gone.
                hwnd = payload
                if not hwnd or not win32gui.IsWindow(hwnd):
                    continue
                title = win32._window_text(hwnd)
                res = executor.close_window(hwnd)
                if res.get("ok"):
                    BUS.publish("action", action="undo", what=f"关闭窗口 {title}")
                    return ToolResult(f'已关闭最近打开的程序窗口 "{title}"。')
                # couldn't close → fall through to next undo candidate
        # nothing on the stack → editor-style undo on the foreground
        executor.press_keys(["ctrl", "z"])
        return ToolResult("无可回收的启动记录；已对当前窗口执行撤销 (Ctrl+Z)。")

    def _t_search_files(self, args: dict) -> ToolResult:
        result = search.search(
            str(args["query"]),
            max_results=int(args.get("max_results", 50)),
            path=args.get("path"),
        )
        return ToolResult(self._fmt(result))

    def _t_tray_click(self, args: dict) -> ToolResult:
        result = tray.click_tray_icon(
            str(args["name"]),
            double=bool(args.get("double", False)),
            button=str(args.get("button", "left")),
        )
        return ToolResult(
            self._fmt(result),
            replay={"tool": "tray_click", "name": str(args["name"]),
                    "double": bool(args.get("double", False)),
                    "button": str(args.get("button", "left"))},
        )

    def _t_launch(self, args: dict) -> ToolResult:
        command = str(args["command"]).strip()
        resolved = self._resolve_launch_target(command)

        def do_launch() -> dict:
            if resolved is not None:
                return apps.launch_target(resolved)
            return executor.launch(command)

        result = verify.run_with_verify(
            "launch",
            do_launch,
            expect=args.get("expect"),
            hwnd=self.hwnd,
            timeout_s=10.0,
        )
        if result.get("ok"):
            # Record the launched app's window (post-launch foreground) so
            # undo_last closes THAT window, not whatever is foreground later.
            launched_hwnd = (result.get("post_state") or {}).get("foreground_hwnd")
            self._undo_stack.append(("app", launched_hwnd))
        if resolved is not None:
            result["resolved_app"] = f'{resolved["name"]} [{resolved["kind"]}]'
        return ToolResult(
            self._fmt(result),
            replay={"tool": "launch", "command": command,
                    "expect": args.get("expect")},
        )

    @staticmethod
    def _resolve_launch_target(command: str) -> dict | None:
        """Map an app display name (e.g. 网易云音乐) to an installed-app target.

        Only kicks in when the command is clearly NOT directly executable:
        not an existing path, not on PATH, no shell syntax. Keeps notepad/calc
        and full commands on the legacy Popen path untouched.
        """
        import shutil
        from pathlib import Path as _P

        if not command or any(ch in command for ch in "\\/|&><\""):
            return None
        if _P(command).exists() or shutil.which(command):
            return None
        base = command.split()[0] if command.isascii() else command
        if command.isascii() and (shutil.which(base) or len(command.split()) > 1):
            return None  # looks like a real command line — don't second-guess it
        candidates = apps.resolve(command)
        return candidates[0] if candidates else None

    def _t_click(self, args: dict) -> ToolResult:
        element_id = args.get("element_id")
        button = args.get("button", "left")
        double = bool(args.get("double", False))
        replay: dict[str, Any] = {
            "tool": "click", "button": button, "double": double,
            "expect": args.get("expect"),
        }

        if element_id is not None:
            element, err = self._resolve_element(int(element_id))
            if element is None:
                return ToolResult(err)
            x, y = element.cx, element.cy
            replay.update({
                "text": element.text, "role": element.role,
                "coords": [x, y], "source": element.source,
            })

            def action() -> dict:
                # UIA fast path for plain left single clicks
                if button == "left" and not double and executor.uia_invoke(element):
                    BUS.publish("action", action="uia_invoke", elem_id=element.elem_id,
                                text=element.text)
                    return {"ok": True, "via": "uia_invoke"}
                return executor.click(x, y, button=button, double=double, hwnd=self.hwnd)
        elif "x" in args and "y" in args:
            x, y = int(args["x"]), int(args["y"])
            replay["coords"] = [x, y]

            def action() -> dict:
                return executor.click(x, y, button=button, double=double, hwnd=self.hwnd)
        else:
            return ToolResult("click 需要 element_id 或 x,y 坐标")

        result = verify.run_with_verify(
            "click", action, expect=args.get("expect"), hwnd=self.hwnd)
        return ToolResult(self._fmt(result), replay=replay)

    def _t_type_text(self, args: dict) -> ToolResult:
        text = str(args["text"])
        element_id = args.get("element_id")
        replay: dict[str, Any] = {
            "tool": "type_text", "text_input": text, "expect": args.get("expect"),
        }
        if element_id is not None:
            element, err = self._resolve_element(int(element_id))
            if element is None:
                return ToolResult(err)
            replay.update({
                "element_text": element.text, "coords": [element.cx, element.cy],
            })
        else:
            element = None

        def action() -> dict:
            if element is not None:
                if executor.uia_set_value(element, text):
                    return {"ok": True, "via": "uia_set_value"}
                executor.click(element.cx, element.cy, hwnd=self.hwnd)
                import time
                time.sleep(0.15)
            return executor.type_text(text)

        result = verify.run_with_verify(
            "type_text", action, expect=args.get("expect"),
            hwnd=self.hwnd, retryable=False)
        return ToolResult(self._fmt(result), replay=replay)

    def _t_hotkey(self, args: dict) -> ToolResult:
        keys = [str(k) for k in args["keys"]]
        result = verify.run_with_verify(
            "hotkey", lambda: executor.press_keys(keys),
            expect=args.get("expect"), hwnd=self.hwnd, retryable=False)
        return ToolResult(
            self._fmt(result),
            replay={"tool": "hotkey", "keys": keys, "expect": args.get("expect")},
        )

    def _t_scroll(self, args: dict) -> ToolResult:
        x, y, amount = int(args["x"]), int(args["y"]), int(args["amount"])
        result = executor.scroll(x, y, amount)
        return ToolResult(
            self._fmt(result),
            replay={"tool": "scroll", "coords": [x, y], "amount": amount},
        )

    def _t_drag(self, args: dict) -> ToolResult:
        x1, y1 = int(args["x1"]), int(args["y1"])
        x2, y2 = int(args["x2"]), int(args["y2"])
        result = executor.drag(x1, y1, x2, y2)
        return ToolResult(
            self._fmt(result),
            replay={"tool": "drag", "from": [x1, y1], "to": [x2, y2]},
        )

    def _t_wait(self, args: dict) -> ToolResult:
        condition = args.get("condition") or {}
        if not isinstance(condition, dict) or not condition:
            return ToolResult("wait 需要 condition 验证条件对象")
        timeout_s = float(args.get("timeout_s", 10.0))
        outcome = verify.check(condition, hwnd=self.hwnd, timeout_s=timeout_s)
        return ToolResult(
            self._fmt(outcome.to_dict()),
            replay={"tool": "wait", "condition": condition, "timeout_s": timeout_s},
        )

    def _t_read_text(self, args: dict) -> ToolResult:
        hwnd = self._resolve_hwnd(args)
        if not hwnd:
            return ToolResult("无目标窗口")
        snapshot = fusion.observe(hwnd, include_icons=False)
        texts = [e.text for e in snapshot.elements if e.text.strip()]
        body = "\n".join(dict.fromkeys(texts))  # de-dupe, keep order
        return ToolResult(body or "（未识别到文字）",
                          summary=f"[已折叠] read_text {len(texts)}条文字")

    def _t_find_image(self, args: dict) -> ToolResult:
        matches = vision_cv.match_template(str(args["template"]))
        if not matches:
            available = vision_cv.list_templates()
            listing = ("可用模板: " + ", ".join(available)) if available else (
                "模板库为空——可用 capture_template 先截存一个图标区域作为模板")
            return ToolResult(f"未找到匹配（检查模板名/阈值）。{listing}")
        lines = [
            f'匹配 "{m.text}" 中心({m.cx},{m.cy}) 置信度{m.confidence:.2f}'
            for m in matches
        ]
        return ToolResult("\n".join(lines))

    def _t_capture_template(self, args: dict) -> ToolResult:
        region = Region(int(args["left"]), int(args["top"]),
                        int(args["width"]), int(args["height"]))
        if region.width < 8 or region.height < 8 or region.width > 600 or region.height > 600:
            return ToolResult("模板区域不合理（宽高需在 8~600 像素之间，应只框住目标图标/按钮）")
        path = vision_cv.save_template(region, str(args["name"]))
        BUS.publish("action", action="capture_template", name=path.stem,
                    size=f"{region.width}x{region.height}")
        return ToolResult(f"已保存模板 \"{path.stem}\"，之后可用 find_image(\"{path.stem}\") 查找")

    def _t_color_probe(self, args: dict) -> ToolResult:
        probe = vision_cv.color_probe(int(args["x"]), int(args["y"]))
        return ToolResult(self._fmt(probe))

    def _t_vlm_describe(self, args: dict) -> ToolResult:
        if not vlm.is_configured():
            return ToolResult("VLM 未配置")
        if all(k in args for k in ("left", "top", "width", "height")):
            region = Region(int(args["left"]), int(args["top"]),
                            int(args["width"]), int(args["height"]))
        else:
            hwnd = self._resolve_hwnd(args)
            if not hwnd:
                return ToolResult("无目标窗口且未指定区域")
            region = window_region(hwnd)
        answer = vlm.describe(region, str(args["question"]))
        return ToolResult(answer)

    def _t_revise_plan(self, args: dict) -> ToolResult:
        steps = args.get("steps") or []
        if not isinstance(steps, list) or not steps:
            return ToolResult("revise_plan 需要非空的 steps 数组")
        self.plan = planner.Plan(steps)  # items may be str or {goal, check}
        # A new approach earns a fresh soft-fail bounce (per-approach, not per-run).
        self._fail_softened = False
        BUS.publish("agent_plan", action="revise", plan=self.plan.snapshot())
        return ToolResult(f"已更新计划：\n{self.plan.render()}")

    def _t_complete_subgoal(self, args: dict) -> ToolResult:
        if self.plan is None or not self.plan.active():
            return ToolResult("当前没有计划，可先调用 revise_plan 制定")
        index = int(args.get("index", -1))
        if not (1 <= index <= len(self.plan.steps)):
            return ToolResult(f"子目标序号 {index} 无效。\n{self.plan.render()}")
        # Subgoal verification gate: if the planner attached a deterministic
        # check, the subgoal can't be marked done until it actually passes.
        check_expect = self.plan.check_for(index)
        if check_expect:
            outcome = verify.check(check_expect, hwnd=self.hwnd, timeout_s=4.0)
            BUS.publish("agent_plan", action="subgoal_check", index=index,
                        verified=outcome.verified, detail=outcome.detail)
            if not outcome.verified:
                return ToolResult(
                    f"子目标 {index} 核验未通过（{outcome.detail}）——尚未真正完成，"
                    "请先达成该子目标再标完成，不要跳步。")
        self.plan.complete(index)
        BUS.publish("agent_plan", action="complete", index=index,
                    plan=self.plan.snapshot())
        if self.plan.all_done():
            return ToolResult(
                f"已完成子目标 {index}（已核验）。所有子目标均完成——若任务确已达成请调用 done。\n"
                f"{self.plan.render()}")
        return ToolResult(f"已完成子目标 {index}{'（已核验）' if check_expect else ''}。\n"
                          f"{self.plan.render()}")

    def _t_remember(self, args: dict) -> ToolResult:
        note = str(args.get("note", "")).strip()
        if not note:
            return ToolResult("remember 需要 note 内容")
        recipe = experience.add(note, app=str(args.get("app", "")).strip(),
                                steps=[note])
        BUS.publish("memory", action="remember", id=recipe["id"], note=note[:120])
        return ToolResult(f"已记住（id={recipe['id']}）: {note}")

    def _t_forget(self, args: dict) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult("forget 需要 query 关键词")
        removed = experience.delete_matching(query)
        if not removed:
            return ToolResult(f'没有匹配 "{query}" 的记忆条目')
        names = "；".join(r.get("task", "")[:60] for r in removed)
        BUS.publish("memory", action="forget", count=len(removed), query=query)
        return ToolResult(f"已删除 {len(removed)} 条记忆: {names}")

    def _t_reuse_skill(self, args: dict) -> ToolResult:
        from ..recorder import trace as trace_mod

        task = str(args.get("task", "")).strip()
        recipe_id = str(args.get("recipe_id", "")).strip()
        script, recipe = experience.reusable_script_for(task or self.task, recipe_id)
        if not script:
            return ToolResult(
                "没有可复用的成功流程脚本（该任务无匹配的已验证历史）。请改用实时操作。")
        from ..config import SCRIPTS_DIR
        script_path = SCRIPTS_DIR / script
        if not script_path.exists():
            return ToolResult(f"复用脚本 {script} 已丢失，请实时操作。")
        BUS.publish("memory", action="reuse", task=(recipe or {}).get("task", task))
        try:
            result = trace_mod.replay(script_path, stop_event=self.stop_event)
        except Exception as exc:
            return ToolResult(f"复用重放异常: {exc}。请改用实时操作。")
        steps = result.get("results", [])
        if result.get("ok"):
            return ToolResult(
                f"✅ 已 0-token 复用上次成功流程（{len(steps)}步全部重放成功）。"
                "请 observe 确认最终结果是否符合预期，符合即可 done。")
        # partial failure → tell the agent where to take over live
        failed = next((s for s in steps if not s.get("ok")), None)
        at = failed.get("index") if failed else len(steps)
        return ToolResult(
            f"⚠️ 复用重放进行到第 {at} 步（{failed.get('tool') if failed else '?'}）失败，"
            f"前 {at-1} 步已执行。请先 observe 看当前界面，从第 {at} 步起改用实时操作完成剩余部分。")

    def _t_done(self, args: dict) -> ToolResult:
        summary = str(args.get("summary", ""))
        return ToolResult(f"任务完成: {summary}", terminal="done")

    def _t_fail(self, args: dict) -> ToolResult:
        reason = str(args.get("reason", ""))
        # Macro recovery: don't give up on the first fail — force one change of
        # approach. Only the second fail (or when disabled) truly terminates.
        if self.replan_on_fail and not self._fail_softened:
            self._fail_softened = True
            BUS.publish("agent_recovery", reason=reason[:200])
            return ToolResult(
                f"在放弃前请再换一种思路尝试一次（你给的失败原因：{reason}）。"
                "建议：重新 observe 确认当前真实界面；检查是否有未处理的弹窗/遮挡；"
                "能否改用 run_shell 直接命令行完成；是否漏看了某个元素或其它窗口。"
                "若确认所有可行办法都已尝试且无解，再次调用 fail 即真正结束。",
                terminal=None,
            )
        return ToolResult(f"任务失败: {reason}", terminal="fail")
