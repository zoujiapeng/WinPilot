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
import re
import threading
import time
import uuid
from typing import Any

from ..config import CONFIG
from ..memory import experience
from ..perception import fusion, vlm
from ..utils.log import BUS, logger
from . import playbooks, prompts, session as session_mod
from .planner import Plan, make_plan, planning_enabled, should_plan_upfront
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

    def __init__(self, task: str, stop_event: threading.Event | None = None,
                 session_context: list | None = None) -> None:
        self.task = task
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.stop_event = stop_event or threading.Event()
        self.session = ToolSession(stop_event=self.stop_event)
        # Prior finished tasks this session (list[SessionEntry]) for continuity.
        self.session_context = session_context or []
        self.messages: list[dict] = []
        self._foldable: list[tuple[int, str]] = []  # (message index, summary line)
        self._steps_log: list[str] = []             # action recipe for experience memory
        self._hint_ids: list[str] = []              # injected recipe ids (score feedback)
        self._injected_playbooks: set[str] = set()  # avoid double-injecting playbooks
        self._had_verified_action = False           # any action passed external verify
        self._last_verify_ok: bool | None = None    # most recent verify outcome
        self._done_rejects = 0                       # done-gate bounce count
        self._done_unverified = False                # forced-accept after max rejects
        self._done_verified = False                  # done-gate judge genuinely passed
        self._recent_results: list[tuple[str, str]] = []  # tool outputs as done-gate evidence
        self._escalate_next = False                  # use stronger model on next _chat
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
        model = api.get("model", "deepseek-v4-flash")
        # Escalate ONE decision to a stronger model when we just got stuck
        # (model-tier mitigation): the weak model flails, the strong model
        # picks the next move, then we drop back to cheap.
        if self._escalate_next:
            self._escalate_next = False
            esc = CONFIG.get("agent", "escalation_model", default="")
            if esc:
                logger.info("本步升级到更强模型: %s", esc)
                model = esc
            else:
                logger.warning("卡住需升级模型，但 agent.escalation_model 未配置，仍用 %s", model)
        last_exc: Exception | None = None
        for attempt in range(1, _API_RETRIES + 1):
            try:
                return client.chat.completions.create(
                    model=model,
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
    def _record_step(self, name: str, args: dict, replay: dict | None = None) -> None:
        """Log an intent-level step for experience memory.

        Prefer replay metadata (target text / command) over raw args: element
        ids and absolute coordinates are meaningless in a future session, while
        "click 保存(S)" generalizes across resolutions and layouts.
        """
        if name not in _ACTION_TOOLS:
            return
        meta = replay or {}
        line = ""
        if name == "click":
            target = meta.get("text") or ""
            line = f'click "{target}"' if target else (
                f'click ({meta["coords"][0]},{meta["coords"][1]})'
                if meta.get("coords") else "click")
        elif name == "type_text":
            text = str(meta.get("text_input") or args.get("text") or "")[:60]
            where = meta.get("element_text") or ""
            line = f'type_text "{text}"' + (f' 在"{where}"' if where else "")
        elif name == "hotkey":
            line = f'hotkey {"+".join(meta.get("keys") or args.get("keys") or [])}'
        elif name in ("launch", "run_shell"):
            line = f'{name} {str(meta.get("command") or args.get("command") or "")[:120]}'
        elif name == "focus_window":
            line = f'focus_window "{meta.get("title") or ""}"'
        elif name == "tray_click":
            line = f'tray_click "{meta.get("name") or args.get("name") or ""}"'
        elif name == "wait":
            cond = meta.get("condition") or args.get("condition") or {}
            line = f"wait {json.dumps(cond, ensure_ascii=False)}"
        if not line:  # scroll/drag stay positional by nature
            compact = {k: v for k, v in args.items() if k != "expect"}
            line = f"{name}({json.dumps(compact, ensure_ascii=False)})"
        self._steps_log.append(line)

    def _track_verification(self, tool_name: str, result_text: str) -> None:
        """Note external verify outcomes so a run whose actions never verified
        can't be recorded as trusted experience (anti fake-done)."""
        if tool_name not in _ACTION_TOOLS:
            return
        try:
            data = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(data, dict) and "verified" in data:
            ok = bool(data["verified"])
            self._last_verify_ok = ok
            if ok:
                self._had_verified_action = True

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
            self._record_step(tc.function.name, args, result.replay)
            self._track_verification(tc.function.name, result.text)
            # Keep recent tool outputs as evidence for the done-gate judge
            # (esp. run_shell stdout / verify results — deterministic proof
            # the GUI screen can't show).
            if tc.function.name in ("run_shell", "run_detached", "search_files",
                                    "read_text", "find_app", "find_program") or \
                    '"verified"' in (result.text or ""):
                self._recent_results.append((tc.function.name, (result.text or "")[:400]))
                self._recent_results = self._recent_results[-6:]
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

    # ------------------------------------------------------------- done-gate
    def _verify_done(self, client, steps_left: int = 99) -> bool:
        """Independently verify a ``done`` claim before accepting it (kills
        "fake done"). Re-observes the real screen and asks a skeptical judge
        call whether the task is genuinely complete. Returns True to accept,
        False to bounce the agent back to keep working.

        Fail-open: judge errors / unparseable output accept the done (never
        deadlock). Capped at config done_max_rejects bounces, after which the
        done is accepted but flagged unverified (won't be trusted as a skill).
        """
        if not CONFIG.get("agent", "verify_done", default=True):
            return True
        max_rejects = int(CONFIG.get("agent", "done_max_rejects", default=2))
        if self._done_rejects >= max_rejects or steps_left <= 2:
            # out of bounce budget (or out of step budget) → accept, mark unverified
            self._done_unverified = True
            return True

        # Fresh perception of the real final state (don't trust the model's belief).
        screen = ""
        try:
            hwnd = self.session.hwnd
            snap = fusion.observe(hwnd, include_icons=False) if hwnd else self.session.snapshot
            screen = snap.to_text(50) if snap else ""
        except Exception as exc:  # perception must never break the gate
            logger.debug("done 核验 observe 失败: %s", exc)

        recent = "；".join(self._steps_log[-6:]) or "(无动作记录)"
        evidence = "\n".join(f"- {name}: {out}" for name, out in self._recent_results) \
            or "(无)"
        api = CONFIG.get("api", default={})
        judge_model = (CONFIG.get("agent", "escalation_model", default="")
                       or api.get("done_judge_model") or api.get("model", "deepseek-v4-flash"))
        sys_p = (
            "你是严格、怀疑的任务完成核验员。判断用户任务是否【真正达成】，不要轻信 agent 的自述。"
            "重要：文件/文件夹/进程/命令类任务，以【确定性证据】(run_shell 输出、Test-Path/Exists 结果、"
            "verified 字段)为准——这类结果在桌面/后台,GUI 界面上【本来就看不到】,"
            "**绝不能因为‘最终界面没显示’就判未完成**。只有可视交互(网页填表/界面内容)才看界面。"
            '只输出 JSON: {"done": true/false, "reason": "简短中文理由"}。')
        user_p = (
            f"用户任务：{self.task}\n\n"
            f"agent 声称完成：{self.result_text[:200]}\n\n"
            f"已执行动作(最近)：{recent}\n\n"
            f"确定性证据(工具真实输出/验证结果)：\n{evidence}\n\n"
            f"当前前台界面元素(仅供参考,文件/进程类勿据此判负)：\n{screen[:1000]}")
        try:
            resp = client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_p}],
                temperature=0.0, max_tokens=400,
            )
            raw = resp.choices[0].message.content or ""
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            verdict = json.loads(m.group(0)) if m else {}
            is_done = bool(verdict.get("done", True))  # unparseable → accept
            reason = str(verdict.get("reason", ""))[:200]
        except Exception as exc:  # fail-open — never deadlock on judge errors
            logger.warning("done 核验调用失败，放行: %s", exc)
            return True

        BUS.publish("done_check", run_id=self.run_id, passed=is_done, reason=reason)
        if is_done:
            self._done_verified = True  # an independent judge confirmed success
            return True
        self._done_rejects += 1
        self.messages.append({
            "role": "user",
            "content": f"[完成核验未通过] {reason}。任务尚未真正达成，请不要直接调 done——"
                       "先把缺的部分补完（必要时重新 observe / 用确定性 expect 验证），完成后再调 done。",
        })
        logger.info("done 被核验拦截(第%d次): %s", self._done_rejects, reason)
        return False

    # --------------------------------------------------------------- planning
    def _maybe_plan_upfront(self, client) -> None:
        """For clearly multi-step tasks, decompose once and inject the plan into
        the stable context prefix (cache-friendly — added before the loop, never
        rewritten). Trivial tasks skip this to avoid the extra LLM call."""
        if not should_plan_upfront(self.task):
            return
        api = CONFIG.get("api", default={})
        steps = make_plan(client, api.get("model", "deepseek-v4-flash"), self.task)
        if not steps:
            return
        self.session.plan = Plan(steps)
        self.messages.append({"role": "user", "content": self.session.plan.render()})
        BUS.publish("agent_plan", run_id=self.run_id, action="init",
                    plan=self.session.plan.snapshot())

    # ------------------------------------------------------------ world model
    def _surface_world_advisory(self) -> None:
        """Append a one-line world advisory to the tail of history (cache-safe,
        append-only) only when there's something worth telling the model, and
        publish compact world state for the UI/trace regardless."""
        if not self.session.world_enabled:
            return
        BUS.publish("world", run_id=self.run_id, **self.session.world.state())
        note = self.session.world.advisory()
        if note:
            self.messages.append({"role": "user", "content": f"[系统观察] {note}"})
        self._maybe_structured_replan()

    def _maybe_structured_replan(self) -> None:
        """On the FIRST detection of being stuck, inject a structured reflection
        that forces a real change of approach (deeper than a one-line nudge):
        name the blocker, what's been tried, and a different method — and revise
        the plan. Edge-triggered (once per stuck episode) to avoid spam."""
        if not self.session.world_enabled:
            return
        stuck, why = self.session.world.take_stuck_signal()
        if not stuck:
            return
        plan_hint = ("用 revise_plan 重列一个【不同思路】的计划"
                     if planning_enabled() else "明确换一个完全不同的方法")
        self.messages.append({
            "role": "user",
            "content": (
                f"[强制重规划] 检测到卡住：{why}。停止重复当前做法，先想清楚再动：\n"
                f"1) 现在卡在哪一步/哪个子目标？2) 已经试过什么、为什么没用？"
                f"3) 有什么【完全不同】的办法（换工具/换路径/run_shell 命令行/find_program/重新 observe）？\n"
                f"然后{plan_hint}并按新计划执行。"),
        })
        BUS.publish("agent_recovery", run_id=self.run_id, kind="structured_replan", reason=why)
        logger.info("触发结构化重规划: %s", why)
        self._escalate_next = True  # let a stronger model pick the new approach

    def _maybe_inject_playbook(self) -> None:
        """Mid-run playbook rescue: if the current window title matches a
        playbook trigger (e.g. a save-as dialog appeared for a task whose text
        never mentioned saving), inject that playbook once."""
        title = self.session.snapshot.title if self.session.snapshot else ""
        book = playbooks.for_window_title(title, exclude=self._injected_playbooks)
        if book is None:
            return
        self._injected_playbooks.add(book.name)
        self.messages.append({
            "role": "user",
            "content": f"[情境提示] 检测到「{title}」相关界面，适用以下方法：\n{book.content}",
        })
        BUS.publish("playbook", run_id=self.run_id, name=book.name, trigger=title[:60])
        logger.info("运行中注入 playbook: %s (窗口: %s)", book.name, title[:40])

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        self.status = "running"
        max_steps = int(CONFIG.get("agent", "max_steps", default=40))
        plan_on = planning_enabled()
        hints, self._hint_ids = experience.hints_with_ids(self.task)
        matched_books = playbooks.for_task(self.task)
        self._injected_playbooks = {b.name for b in matched_books}
        system = prompts.system_prompt(
            experience_hints=hints, vlm_enabled=vlm.is_configured(),
            planning_enabled=plan_on,
            playbook_text=playbooks.render_section(matched_books))
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self.task},
        ]
        # Session continuity: let referential follow-ups ("再来一次" etc.) resolve
        # against prior tasks this session. Injected right after the task so it's
        # in the cache-friendly prefix; gated by config.
        if self.session_context and CONFIG.get("agent", "session_memory", default=True):
            ctx = session_mod.render_context(self.session_context)
            if ctx:
                self.messages.append({"role": "user", "content": ctx})
        # Proactive reuse: when this task strongly matches a past run that left a
        # verified replay script, direct the agent to reuse it first (it's the
        # whole point of skills; replay failure auto-falls-back to live actions).
        try:
            script, recipe = experience.reusable_script_for(self.task)
            if script and recipe:
                self.messages.append({
                    "role": "user",
                    "content": (
                        f"⚡ 本任务与上次成功完成的「{recipe['task']}」高度一致，已有可复用流程脚本。"
                        f"**第一步请直接调用 reuse_skill 复用**（0-token 重放上次步骤）；"
                        "若重放中途某步失败，它会告诉你卡在哪，你再从那步起实时操作。"),
                })
                BUS.publish("memory", action="reuse_suggested", task=recipe["task"])
        except Exception as exc:
            logger.debug("复用建议注入失败: %s", exc)
        tools = tool_schemas(planning=plan_on)
        BUS.publish("agent_start", run_id=self.run_id, task=self.task,
                    max_steps=max_steps, has_experience=bool(hints))
        try:
            client = _client()
            self._maybe_plan_upfront(client)
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
                if terminal == "done" and not self._verify_done(client, steps_left=max_steps - step):
                    continue  # done-gate rejected the claim → keep working
                if terminal:
                    self.status = terminal
                    break
                self._surface_world_advisory()
                self._maybe_inject_playbook()
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
            # Trusted (→ reusable skill + linked script) when EITHER the done-gate
            # judge independently confirmed success, OR an action passed external
            # verify; and never when done was force-accepted after max rejects.
            verified_run = (
                (self._done_verified
                 or (self._had_verified_action and self._last_verify_ok is not False))
                and not self._done_unverified)
            # Reuse-like-scripts: a trusted success becomes a replayable .wps.json
            # linked to the recipe, so a future identical task can reuse_skill it.
            script_name = ""
            if verified_run:
                try:
                    from ..recorder import trace as _trace
                    script_path = _trace.export_script(self.run_id)
                    script_name = script_path.name
                except (FileNotFoundError, ValueError) as exc:
                    logger.debug("成功流程无可重放步骤，跳过导出: %s", exc)
                except OSError as exc:
                    logger.warning("成功流程导出脚本失败(IO): %s", exc)
            experience.record(self.task, app_hint, self._steps_log,
                              verified=verified_run, script=script_name)
        # Score feedback for whatever recipes were injected this run.
        if self.status in ("done", "fail", "error"):
            experience.feedback(self._hint_ids, success=(self.status == "done"))

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
