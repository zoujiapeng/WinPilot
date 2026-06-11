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
from ..perception import fusion, search, tray, vision_cv, vlm, win32
from ..utils.log import BUS, logger
from ..utils.screenshot import Region, window_region

_EXPECT_SCHEMA = {
    "type": "object",
    "description": (
        '验证条件，可组合: {"text_appears":"另存为"} {"text_gone":"..."} '
        '{"window_appears":"标题子串"} {"window_gone":"..."} {"screen_stable":true} '
        '{"color_match":{"x":1,"y":2,"rgb":[0,120,215],"tolerance":40}}'
    ),
}


def tool_schemas() -> list[dict]:
    """OpenAI tools array; vlm_describe only when a VLM is configured."""
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
            "启动程序（exe名/完整路径/shell命令），如 notepad、calc",
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
            "color_probe",
            "读取屏幕某点的颜色（平均色+主色，hex），判断按钮状态/主题/进度条",
            {"x": {"type": "integer"}, "y": {"type": "integer"}},
            required=["x", "y"],
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

    def __init__(self) -> None:
        self.snapshot: fusion.Snapshot | None = None
        self.hwnd: int | None = None

    # ----------------------------------------------------------- dispatch
    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolResult(f"未知工具: {name}")
        try:
            return handler(args)
        except Exception as exc:
            logger.exception("工具 %s 执行异常", name)
            return ToolResult(f"工具 {name} 执行异常: {exc}")

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
        command = str(args["command"])
        result = verify.run_with_verify(
            "launch",
            lambda: executor.launch(command),
            expect=args.get("expect"),
            hwnd=self.hwnd,
            timeout_s=10.0,
        )
        return ToolResult(
            self._fmt(result),
            replay={"tool": "launch", "command": command,
                    "expect": args.get("expect")},
        )

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
            return ToolResult("未找到匹配（检查模板名/阈值）")
        lines = [
            f'匹配 "{m.text}" 中心({m.cx},{m.cy}) 置信度{m.confidence:.2f}'
            for m in matches
        ]
        return ToolResult("\n".join(lines))

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

    def _t_done(self, args: dict) -> ToolResult:
        summary = str(args.get("summary", ""))
        return ToolResult(f"任务完成: {summary}", terminal="done")

    def _t_fail(self, args: dict) -> ToolResult:
        reason = str(args.get("reason", ""))
        return ToolResult(f"任务失败: {reason}", terminal="fail")
