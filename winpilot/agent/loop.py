"""Agent loop: deepseek tool-calling with WinPilot-specific adaptations.

DeepSeek adaptations (per official docs + live testing):
- ``reasoning_content`` must be stripped before sending history back
- append-only history keeps the prompt-cache prefix warm; old bulky tool
  results (observe / read_text / list_windows) are folded to one-line
  summaries, keeping only the 2 most recent full observations
- malformed tool-call JSON is bounced back to the model for self-repair
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from ..config import CONFIG
from ..memory import experience
from ..perception import vlm
from ..utils.log import BUS, logger
from . import prompts
from .tools import ToolSession, tool_schemas

_KEEP_FULL_OBSERVATIONS = 2
_API_RETRIES = 3
# Fold old observations only past this size (~12K tokens × ~4 chars/token).
# Below it, history stays append-only so the DeepSeek prefix cache hits ~100%.
_CONTEXT_FOLD_THRESHOLD_CHARS = 48000
# DeepSeek pricing (¥ per 1M tokens). Adjust if the published rates change.
_PRICE_CACHE_MISS_PER_M = 2.0
_PRICE_CACHE_HIT_PER_M = 0.2
_PRICE_OUTPUT_PER_M = 8.0
_ACTION_TOOLS = {"launch", "click", "type_text", "hotkey", "scroll", "drag",
                 "focus_window", "wait", "tray_click", "run_shell"}


def _client():
    from openai import OpenAI

    api = CONFIG.get("api", default={})
    if not api.get("api_key"):
        raise RuntimeError("config.json 缺少 api.api_key")
    return OpenAI(base_url=api["base_url"], api_key=api["api_key"], timeout=120)


def _sanitize_assistant(msg) -> dict[str, Any]:
    """Assistant message for history — WITHOUT reasoning_content."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


class AgentRunner:
    """One task execution. Designed to run on a background thread."""

    def __init__(self, task: str, stop_event: threading.Event | None = None) -> None:
        self.task = task
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.stop_event = stop_event or threading.Event()
        self.session = ToolSession()
        self.messages: list[dict] = []
        self._foldable: list[tuple[int, str]] = []  # (message index, summary line)
        self._steps_log: list[str] = []             # action recipe for experience memory
        self.status = "pending"                     # running/done/fail/reply/stopped/error
        self.result_text = ""
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                            "cache_hit_tokens": 0, "cost_cny": 0.0}
        self._interjections: list[str] = []          # mid-run user hints
        self._interject_lock = threading.Lock()

    def interject(self, text: str) -> None:
        """Queue a user hint to inject before the agent's next decision."""
        text = (text or "").strip()
        if text:
            with self._interject_lock:
                self._interjections.append(text)

    def _drain_interjections(self) -> None:
        """Append any queued user hints as user messages (valid after tool
        results) so the model sees them before the next API call."""
        with self._interject_lock:
            pending = self._interjections
            self._interjections = []
        for text in pending:
            self.messages.append({
                "role": "user",
                "content": f"[用户实时提示] {text}\n（这是用户在旁观察后给的建议，"
                           f"请重新考虑当前思路、采纳更好的做法，不要机械照搬字面）",
            })
            BUS.publish("chat", run_id=self.run_id, role="user", content=text,
                        interject=True)
            logger.info("收到用户插话: %s", text[:80])

    # ------------------------------------------------------------- history
    def _append_tool_result(self, tool_call_id: str, result_text: str,
                            summary: str = "") -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": result_text})
        if summary:
            self._foldable.append((len(self.messages) - 1, summary))
            self._fold_old()

    def _fold_old(self) -> None:
        """Compress old bulky observations — but ONLY when the context is large.

        Folding mutates a historical message, which breaks DeepSeek's prefix
        cache from that point once. So we avoid it entirely while the context
        is small (the common case → near-100% cache hits), and only start
        folding the oldest observations once total size crosses a threshold.
        The newest _KEEP_FULL_OBSERVATIONS are always kept full.
        """
        def total_chars() -> int:
            return sum(len(str(m.get("content") or "")) for m in self.messages)

        while (
            len(self._foldable) > _KEEP_FULL_OBSERVATIONS
            and total_chars() > _CONTEXT_FOLD_THRESHOLD_CHARS
        ):
            idx, summary = self._foldable.pop(0)
            if not self.messages[idx]["content"].startswith("[已折叠]"):
                self.messages[idx]["content"] = summary

    # ----------------------------------------------------------------- api
    def _chat(self, client, tools: list[dict]):
        api = CONFIG.get("api", default={})
        last_exc: Exception | None = None
        for attempt in range(1, _API_RETRIES + 1):
            try:
                return client.chat.completions.create(
                    model=api.get("model", "deepseek-v4-flash"),
                    messages=self.messages,
                    tools=tools,
                    temperature=float(api.get("temperature", 0.3)),
                    max_tokens=int(api.get("max_tokens", 4096)),
                )
            except Exception as exc:
                last_exc = exc
                logger.warning("API 调用失败 (第%d次): %s", attempt, exc)
                if attempt < _API_RETRIES:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"API 连续 {_API_RETRIES} 次失败: {last_exc}")

    def _track_usage(self, resp) -> None:
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        cache_miss = max(0, prompt - cache_hit)
        step_cost = (
            cache_miss / 1_000_000 * _PRICE_CACHE_MISS_PER_M
            + cache_hit / 1_000_000 * _PRICE_CACHE_HIT_PER_M
            + completion / 1_000_000 * _PRICE_OUTPUT_PER_M
        )
        self.usage_total["prompt_tokens"] += prompt
        self.usage_total["completion_tokens"] += completion
        self.usage_total["cache_hit_tokens"] += cache_hit
        self.usage_total["cost_cny"] = round(self.usage_total["cost_cny"] + step_cost, 4)
        BUS.publish("agent_usage", run_id=self.run_id,
                    step_usage={
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "cache_hit_tokens": cache_hit,
                        "cost_cny": round(step_cost, 4),
                    },
                    total=dict(self.usage_total))

    # ---------------------------------------------------------------- steps
    def _record_step(self, name: str, args: dict) -> None:
        if name not in _ACTION_TOOLS:
            return
        compact = {k: v for k, v in args.items() if k != "expect"}
        self._steps_log.append(f"{name}({json.dumps(compact, ensure_ascii=False)})")

    def _handle_tool_calls(self, tool_calls) -> str | None:
        """Dispatch each tool call; returns terminal status if any."""
        for tc in tool_calls:
            if self.stop_event.is_set():
                return "stopped"
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                error = f"工具参数 JSON 解析失败: {exc}。请重新调用并给出合法 JSON。"
                self._append_tool_result(tc.id, error)
                BUS.publish("agent_tool", run_id=self.run_id, tool=tc.function.name,
                            error=str(exc))
                continue

            started = time.monotonic()
            result = self.session.dispatch(tc.function.name, args)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._record_step(tc.function.name, args)
            BUS.publish(
                "agent_tool",
                run_id=self.run_id,
                tool=tc.function.name,
                args=args,
                result_preview=result.text[:300],
                elapsed_ms=elapsed_ms,
                terminal=result.terminal,
                replay=result.replay,
            )
            self._append_tool_result(tc.id, result.text, summary=result.summary)
            if result.terminal:
                self.result_text = result.text
                return result.terminal
        return None

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        self.status = "running"
        max_steps = int(CONFIG.get("agent", "max_steps", default=40))
        hints = experience.hints_for(self.task)
        system = prompts.system_prompt(
            experience_hints=hints, vlm_enabled=vlm.is_configured())
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self.task},
        ]
        tools = tool_schemas()
        BUS.publish("agent_start", run_id=self.run_id, task=self.task,
                    max_steps=max_steps, has_experience=bool(hints))
        try:
            client = _client()
            for step in range(1, max_steps + 1):
                if self.stop_event.is_set():
                    self.status = "stopped"
                    break
                self._drain_interjections()
                resp = self._chat(client, tools)
                self._track_usage(resp)
                msg = resp.choices[0].message
                reasoning = getattr(msg, "reasoning_content", None) or ""
                BUS.publish(
                    "agent_step",
                    run_id=self.run_id,
                    step=step,
                    reasoning=reasoning[:800],
                    content=(msg.content or "")[:800],
                    n_tool_calls=len(msg.tool_calls or []),
                )
                self.messages.append(_sanitize_assistant(msg))

                if not msg.tool_calls:
                    # plain text reply — treat as the final answer to the user
                    self.status = "reply"
                    self.result_text = msg.content or ""
                    BUS.publish("chat", run_id=self.run_id, role="assistant",
                                content=self.result_text)
                    break

                terminal = self._handle_tool_calls(msg.tool_calls)
                if terminal:
                    self.status = terminal
                    break
            else:
                self.status = "fail"
                self.result_text = f"达到最大步数 {max_steps} 仍未完成"
        except Exception as exc:
            logger.exception("Agent 运行异常")
            self.status = "error"
            self.result_text = str(exc)
            BUS.publish("agent_error", run_id=self.run_id, error=str(exc))

        if self.status == "done":
            app_hint = self.session.snapshot.title if self.session.snapshot else ""
            experience.record(self.task, app_hint, self._steps_log)

        BUS.publish(
            "agent_done",
            run_id=self.run_id,
            status=self.status,
            result=self.result_text[:500],
            steps=len(self._steps_log),
            usage=dict(self.usage_total),
        )
        return {
            "run_id": self.run_id,
            "status": self.status,
            "result": self.result_text,
            "usage": dict(self.usage_total),
        }
