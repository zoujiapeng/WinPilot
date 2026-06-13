/* WinPilot front-end: WebSocket event stream + chat + settings panel. */
"use strict";

const $ = (id) => document.getElementById(id);
const chatList = $("chat-list");
const logList = $("log-list");

let config = null;
let running = false;

/* ============================== utils ============================== */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function fmtTs(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toTimeString().slice(0, 8);
}
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_e) { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}
const patchConfig = (patch) =>
  api("/api/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  }).then((cfg) => { config = cfg; renderBadgesFromConfig(); });

/* ============================== chat ============================== */
function addChat(role, content, extra) {
  const msg = el("div", `msg ${role}`);
  if (extra && extra.reasoning) {
    const details = el("details", "reasoning");
    details.appendChild(el("summary", "", "思考过程"));
    details.appendChild(el("div", "", extra.reasoning));
    msg.appendChild(details);
  }
  const body = el("div", "", content);
  if (extra && extra.statusClass) body.classList.add(extra.statusClass);
  msg.appendChild(body);
  chatList.appendChild(msg);
  chatList.scrollTop = chatList.scrollHeight;
}

/* ============================== logs ============================== */
const MAX_LOG_LINES = 1500;
function addLog(tagText, tagClass, body, ts) {
  const line = el("div", "log-line");
  line.appendChild(el("span", "ts", fmtTs(ts)));
  line.appendChild(el("span", `tag ${tagClass}`, tagText));
  const span = el("span", "body");
  if (body instanceof Node) span.appendChild(body); else span.textContent = body;
  line.appendChild(span);
  logList.appendChild(line);
  while (logList.childNodes.length > MAX_LOG_LINES) logList.removeChild(logList.firstChild);
  if ($("log-autoscroll").checked) logList.scrollTop = logList.scrollHeight;
}
const okMark = (ok) => {
  const s = el("span", ok ? "ok-mark" : "fail-mark", ok ? " ✓" : " ✗");
  return s;
};

/* ========================= event rendering ========================= */
function renderEvent(ev) {
  switch (ev.type) {
    case "ping": return;
    case "chat":
      if (ev.role === "assistant") addChat("assistant", ev.content);
      return;
    case "agent_start":
      running = true; updateRunBadge();
      addLog("agent", "agent", `任务开始 [${ev.run_id}] 最大步数=${ev.max_steps}` +
        (ev.has_experience ? " (注入了历史经验)" : ""), ev.ts);
      return;
    case "agent_step": {
      if (ev.reasoning || ev.content) {
        const text = ev.content || "(调用工具…)";
        addChat("assistant", `步骤 ${ev.step}: ${text}`,
          ev.reasoning ? { reasoning: ev.reasoning } : null);
      }
      addLog("agent", "agent", `步骤 ${ev.step}: ${ev.n_tool_calls} 个工具调用`, ev.ts);
      return;
    }
    case "agent_tool": {
      const frag = document.createDocumentFragment();
      const argsStr = ev.args ? JSON.stringify(ev.args) : "";
      frag.appendChild(document.createTextNode(
        `${ev.tool}(${argsStr.length > 120 ? argsStr.slice(0, 117) + "…" : argsStr}) ` +
        `${ev.elapsed_ms != null ? ev.elapsed_ms + "ms" : ""}`));
      if (ev.error) frag.appendChild(el("span", "fail-mark", ` 参数错误: ${ev.error}`));
      addLog("tool", "action", frag, ev.ts);
      return;
    }
    case "perception": {
      const dims = Object.entries(ev.dims || {})
        .map(([k, v]) => `${k}:${v.count}个/${v.ms}ms`).join(" ");
      const frag = document.createDocumentFragment();
      frag.appendChild(document.createTextNode(`observe "${ev.title}" 共${ev.total}元素 `));
      frag.appendChild(el("span", "dim-src", dims));
      addLog("感知", "perception", frag, ev.ts);
      return;
    }
    case "action":
      addLog("动作", "action",
        `${ev.action} ${JSON.stringify(Object.fromEntries(
          Object.entries(ev).filter(([k]) => !["type", "ts", "action"].includes(k))))}`, ev.ts);
      return;
    case "verify": {
      const frag = document.createDocumentFragment();
      frag.appendChild(document.createTextNode(
        `${ev.action} 第${ev.attempt}次 [${ev.method || "?"}] ${ev.detail} ${ev.elapsed_ms}ms`));
      frag.appendChild(okMark(ev.verified));
      addLog("验证", "verify", frag, ev.ts);
      return;
    }
    case "agent_plan": {
      const steps = ev.plan || [];
      const done = steps.filter((s) => s.done).length;
      const frag = document.createDocumentFragment();
      const label = ev.action === "complete" ? `完成子目标 ${ev.index}` :
        ev.action === "revise" ? "更新计划" : "制定计划";
      frag.appendChild(document.createTextNode(`${label} (${done}/${steps.length})`));
      const ul = el("div", "plan-todo");
      steps.forEach((s, i) => {
        ul.appendChild(el("div", s.done ? "todo-done" : "todo-pending",
          `${s.done ? "☑" : "☐"} ${i + 1}. ${s.goal}`));
      });
      frag.appendChild(ul);
      addLog("计划", "agent", frag, ev.ts);
      return;
    }
    case "world": {
      // Only surface noteworthy state — avoid per-step spam.
      if ((ev.repeated || 0) >= 2 || (ev.identical_observes || 0) >= 1) {
        addLog("世界", "verify",
          `重复动作×${ev.repeated} 无变化×${ev.identical_observes}` +
          (ev.last_change ? ` | ${ev.last_change}` : ""), ev.ts);
      }
      return;
    }
    case "agent_recovery":
      addLog("恢复", "error", `首次 fail 被拦截，要求换思路再试: ${ev.reason || ""}`, ev.ts);
      return;
    case "playbook":
      addLog("攻略", "agent", `运行中注入 playbook: ${ev.name}（触发窗口: ${ev.trigger}）`, ev.ts);
      return;
    case "memory":
      addLog("记忆", "agent",
        ev.action === "remember" ? `记住: ${ev.note}` :
        ev.action === "forget" ? `忘掉 ${ev.count} 条 (匹配"${ev.query}")` :
        ev.action === "reuse" ? `复用上次成功流程: ${ev.task}` :
        JSON.stringify(ev), ev.ts);
      refreshMemory();
      return;
    case "session":
      refreshSession();
      return;
    case "agent_usage": {
      const t = ev.total || {};
      const cost = t.cost_cny != null ? ` ¥${Number(t.cost_cny).toFixed(4)}` : "";
      $("badge-tokens").textContent =
        `tokens: ${t.prompt_tokens || 0}/${t.completion_tokens || 0} (cache ${t.cache_hit_tokens || 0})${cost}`;
      return;
    }
    case "agent_done": {
      running = false; updateRunBadge();
      const cls = ev.status === "done" ? "status-done" :
        (ev.status === "reply" ? "" : "status-fail");
      addChat("system", `—— 运行结束: ${ev.status} ——`);
      if (ev.result && ev.status !== "reply") addChat("assistant", ev.result, { statusClass: cls });
      addLog("agent", ev.status === "done" ? "agent" : "error",
        `结束 status=${ev.status} 动作步数=${ev.steps}`, ev.ts);
      refreshTraces();
      refreshSession();
      return;
    }
    case "agent_error":
      addLog("错误", "error", ev.error, ev.ts);
      return;
    case "replay": {
      let text;
      if (ev.state === "step") {
        const frag = document.createDocumentFragment();
        frag.appendChild(document.createTextNode(`步骤${ev.index} ${ev.tool}`));
        frag.appendChild(okMark(ev.ok));
        if (ev.detail && ev.detail.error) frag.appendChild(el("span", "fail-mark", " " + ev.detail.error));
        addLog("重放", "replay", frag, ev.ts);
        return;
      }
      if (ev.state === "start") text = `开始重放 ${ev.script} (${ev.total}步, 0 token)`;
      else if (ev.state === "done") text = `重放${ev.ok ? "成功" : "失败"}`;
      else text = JSON.stringify(ev);
      addLog("重放", "replay", text, ev.ts);
      if (ev.state === "done") { running = false; updateRunBadge(); }
      if (ev.state === "start") { running = true; updateRunBadge(); }
      return;
    }
    case "trace":
      addLog("trace", "agent", `${ev.state} ${ev.script || ev.run_id || ""}`, ev.ts);
      if (ev.state === "exported") refreshScripts();
      return;
    case "config":
      return;
    default:
      addLog(ev.type, "agent", JSON.stringify(ev), ev.ts);
  }
}

/* ============================ websocket ============================ */
function connectWs() {
  const ws = new WebSocket(`ws://${location.host}/ws/events`);
  ws.onmessage = (e) => {
    try { renderEvent(JSON.parse(e.data)); } catch (_err) { /* skip bad frame */ }
  };
  ws.onclose = () => setTimeout(connectWs, 1500);
}

/* ============================== badges ============================== */
function updateRunBadge() {
  const badge = $("badge-run");
  badge.textContent = running ? "运行中" : "空闲";
  badge.classList.toggle("running", running);
  $("btn-stop").disabled = !running;
  // Keep send usable while running — it becomes a "steer / interject" button.
  $("btn-send").disabled = false;
  $("btn-send").textContent = running ? "插话提示" : "执行任务";
  $("task-input").placeholder = running
    ? "任务运行中——发一句提示纠偏/补充，它会在下一步看到"
    : "描述任务，如：打开记事本，输入\"你好世界\"，另存为到桌面 test.txt";
}
function renderBadgesFromConfig() {
  if (!config) return;
  $("badge-model").textContent = config.api.model;
  const dims = Object.entries(config.perception)
    .filter(([, v]) => v.enabled)
    .sort((a, b) => a[1].priority - b[1].priority)
    .map(([k]) => k).join("→");
  $("badge-dims").textContent = `维度: ${dims || "无"}`;
}
async function refreshStatus() {
  try {
    const st = await api("/api/status");
    running = st.running; updateRunBadge();
    const ocrBadge = $("badge-ocr");
    const isGpu = st.ocr_provider === "DmlExecutionProvider";
    ocrBadge.textContent = "OCR: " + (st.ocr_provider === "uninitialized" ? "未初始化"
      : isGpu ? "GPU(DML)" : "CPU");
    ocrBadge.classList.toggle("gpu", isGpu);
    $("ocr-provider-line").textContent = `当前 provider: ${st.ocr_provider}`;
    if (typeof st.is_admin === "boolean") {
      const ab = $("badge-admin");
      ab.textContent = "权限: " + (st.is_admin ? "管理员" : "普通");
      ab.classList.toggle("gpu", st.is_admin);
      $("admin-line").textContent = "当前权限: " + (st.is_admin ? "管理员" : "普通用户");
      $("btn-elevate").disabled = st.is_admin;
      if (st.is_admin) $("btn-elevate").textContent = "已是管理员";
    }
    if (typeof st.shell_enabled === "boolean") $("cfg-shell").checked = st.shell_enabled;
    if (st.usage) {
      const cost = st.usage.cost_cny != null ? ` ¥${Number(st.usage.cost_cny).toFixed(4)}` : "";
      $("badge-tokens").textContent =
        `tokens: ${st.usage.prompt_tokens}/${st.usage.completion_tokens} (cache ${st.usage.cache_hit_tokens})${cost}`;
    }
  } catch (_e) { /* server restarting */ }
}

/* ============================= settings ============================= */
const DIM_DESCS = {
  uia: "UIA控件树(最准)", win32: "Win32/Explorer", ocr: "文字识别",
  cv: "图标/颜色/稳定", vlm: "多模态视觉",
};
function renderDims() {
  const wrap = $("dim-list");
  wrap.textContent = "";
  Object.entries(config.perception).forEach(([name, opts]) => {
    const row = el("div", "dim-row");
    const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.checked = !!opts.enabled;
    checkbox.onchange = () => patchConfig({ perception: { [name]: { enabled: checkbox.checked } } });
    const label = el("span", "name", name);
    const priority = el("input"); priority.type = "number"; priority.value = opts.priority;
    priority.min = 1; priority.max = 9; priority.title = "优先级";
    priority.onchange = () => patchConfig({ perception: { [name]: { priority: Number(priority.value) } } });
    row.append(checkbox, label, priority, el("span", "desc", DIM_DESCS[name] || ""));
    wrap.appendChild(row);
  });
}
function fillSettings() {
  renderDims();
  $("cfg-max-steps").value = config.agent.max_steps;
  $("cfg-max-retries").value = config.agent.max_retries;
  $("cfg-dry-run").checked = !!config.agent.dry_run;
  $("cfg-experience").checked = !!config.agent.experience_memory;
  if ($("cfg-no-steal")) $("cfg-no-steal").checked = !!config.agent.no_mouse_steal;
  $("cfg-model").value = config.api.model;
  $("cfg-temperature").value = config.api.temperature;
  $("cfg-vlm-enabled").checked = !!config.vlm.enabled;
  $("cfg-vlm-url").value = config.vlm.base_url || "";
  $("cfg-vlm-model").value = config.vlm.model || "";
  $("cfg-ocr-gpu").checked = !!config.ocr.use_gpu;
}
function bindSettings() {
  $("cfg-max-steps").onchange = (e) => patchConfig({ agent: { max_steps: Number(e.target.value) } });
  $("cfg-max-retries").onchange = (e) => patchConfig({ agent: { max_retries: Number(e.target.value) } });
  $("cfg-dry-run").onchange = (e) => patchConfig({ agent: { dry_run: e.target.checked } });
  $("cfg-experience").onchange = (e) => patchConfig({ agent: { experience_memory: e.target.checked } });
  $("cfg-shell").onchange = (e) => patchConfig({ agent: { shell_enabled: e.target.checked } });
  $("cfg-no-steal").onchange = (e) => patchConfig({ agent: { no_mouse_steal: e.target.checked } });
  $("cfg-model").onchange = (e) => patchConfig({ api: { model: e.target.value } });
  $("cfg-temperature").onchange = (e) => patchConfig({ api: { temperature: Number(e.target.value) } });
  $("cfg-api-key").onchange = (e) => {
    if (e.target.value.trim()) patchConfig({ api: { api_key: e.target.value.trim() } });
    e.target.value = "";
  };
  $("cfg-vlm-enabled").onchange = (e) => patchConfig({
    vlm: { enabled: e.target.checked },
    perception: { vlm: { enabled: e.target.checked } },
  });
  $("cfg-vlm-url").onchange = (e) => patchConfig({ vlm: { base_url: e.target.value.trim() } });
  $("cfg-vlm-model").onchange = (e) => patchConfig({ vlm: { model: e.target.value.trim() } });
  $("cfg-vlm-key").onchange = (e) => {
    if (e.target.value.trim()) patchConfig({ vlm: { api_key: e.target.value.trim() } });
    e.target.value = "";
  };
  $("cfg-ocr-gpu").onchange = (e) => patchConfig({ ocr: { use_gpu: e.target.checked } });
  $("btn-clear-memory").onclick = async () => {
    if (!confirm("确定清空全部经验记忆？")) return;
    await api("/api/memory", { method: "DELETE" });
    addLog("memory", "agent", "经验记忆已清空");
    refreshMemory();
  };
  $("btn-refresh-memory").onclick = refreshMemory;
  $("btn-refresh-session").onclick = refreshSession;
  $("btn-clear-session").onclick = async () => {
    if (!confirm("清空本会话上下文？('再来一次'等指代将失去锚点)")) return;
    await api("/api/session", { method: "DELETE" });
    refreshSession();
  };
  $("btn-add-memory").onclick = async () => {
    const task = prompt("记忆标题（任务/备忘内容）:");
    if (!task || !task.trim()) return;
    const app = prompt("关联应用名（可空）:") || "";
    const stepsRaw = prompt("步骤（每行一条，可空）:") || "";
    const steps = stepsRaw.split("\n").map((s) => s.trim()).filter(Boolean);
    try {
      await api("/api/memory", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: task.trim(), app, steps }),
      });
      addLog("memory", "agent", `已新增记忆: ${task.trim()}`);
      refreshMemory();
    } catch (err) { addLog("memory", "error", `新增失败: ${err.message}`); }
  };
  $("btn-elevate").onclick = async () => {
    if (!confirm("将弹出 UAC 并以管理员身份重启 WinPilot。当前任务会中断，确定？")) return;
    try {
      const r = await api("/api/elevate", { method: "POST" });
      if (r.already_admin) { addLog("权限", "agent", "已是管理员"); return; }
      addChat("system", "正在以管理员身份重启… 若 UAC 通过，请稍候刷新页面。");
    } catch (err) { addChat("system", `提权失败: ${err.message}`); }
  };
}

/* ========================= traces & scripts ========================= */
async function refreshSession() {
  try {
    const items = await api("/api/session");
    const wrap = $("session-list");
    wrap.textContent = "";
    if (!items.length) { wrap.appendChild(el("span", "hint", "（本会话暂无历史）")); return; }
    items.forEach((s) => {
      const row = el("div", "trace-row");
      const cls = s.status === "done" ? "" : "fail-mark";
      row.appendChild(el("span", `t-name ${cls}`, `${s.task}`));
      row.appendChild(el("span", "t-status", s.status));
      wrap.appendChild(row);
    });
  } catch (_e) { /* noop */ }
}

async function refreshMemory() {
  try {
    const recipes = await api("/api/memory");
    const wrap = $("memory-list");
    wrap.textContent = "";
    if (!recipes.length) { wrap.appendChild(el("span", "hint", "（无记忆条目）")); return; }
    recipes.forEach((r) => {
      const row = el("div", "trace-row");
      const stat = r.provisional ? "⚠未验证" :
        (r.uses ? `${r.successes}/${r.uses}成` : "");
      row.appendChild(el("span", "t-name",
        `${r.script ? "⭐ " : ""}${r.task}${r.app ? " · " + r.app : ""} ${stat}`));
      const editBtn = el("button", "mini", "改");
      editBtn.onclick = async () => {
        const task = prompt("标题:", r.task);
        if (task === null) return;
        const steps = prompt("步骤（每行一条）:", (r.steps || []).join("\n"));
        if (steps === null) return;
        try {
          await api(`/api/memory/${r.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              task: task.trim(),
              steps: steps.split("\n").map((s) => s.trim()).filter(Boolean),
            }),
          });
          refreshMemory();
        } catch (err) { addLog("memory", "error", `修改失败: ${err.message}`); }
      };
      const delBtn = el("button", "mini", "删");
      delBtn.onclick = async () => {
        try {
          await api(`/api/memory/${r.id}`, { method: "DELETE" });
          refreshMemory();
        } catch (err) { addLog("memory", "error", `删除失败: ${err.message}`); }
      };
      row.append(editBtn, delBtn);
      wrap.appendChild(row);
    });
  } catch (_e) { /* noop */ }
}

async function refreshTraces() {
  try {
    const traces = await api("/api/traces");
    const wrap = $("trace-list");
    wrap.textContent = "";
    traces.slice(0, 12).forEach((t) => {
      const row = el("div", "trace-row");
      row.appendChild(el("span", "t-name", `${t.run_id} ${t.task ? "· " + t.task : ""}`));
      row.appendChild(el("span", "t-status", `${t.status || "?"} ${t.steps}步`));
      const btn = el("button", "mini", "导出脚本");
      btn.onclick = async () => {
        try {
          const r = await api(`/api/export/${t.run_id}`, { method: "POST" });
          addLog("trace", "agent", `已导出: ${r.script}`);
          refreshScripts();
        } catch (err) { addLog("trace", "error", `导出失败: ${err.message}`); }
      };
      row.appendChild(btn);
      wrap.appendChild(row);
    });
  } catch (_e) { /* noop */ }
}
async function refreshScripts() {
  try {
    const scriptsArr = await api("/api/scripts");
    const wrap = $("script-list");
    wrap.textContent = "";
    scriptsArr.forEach((s) => {
      const row = el("div", "trace-row");
      row.appendChild(el("span", "t-name", `${s.file} (${s.steps}步)`));
      const btn = el("button", "mini", "重放");
      btn.onclick = async () => {
        try {
          await api("/api/replay", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ script: s.file }),
          });
        } catch (err) { addLog("重放", "error", err.message); }
      };
      row.appendChild(btn);
      wrap.appendChild(row);
    });
  } catch (_e) { /* noop */ }
}

/* =============================== main =============================== */
async function main() {
  config = await api("/api/config");
  renderBadgesFromConfig();
  fillSettings();
  bindSettings();
  connectWs();
  refreshStatus();
  refreshTraces();
  refreshScripts();
  refreshMemory();
  refreshSession();
  setInterval(refreshStatus, 4000);

  $("btn-send").onclick = async () => {
    const text = $("task-input").value.trim();
    if (!text) return;
    $("task-input").value = "";
    if (running) {
      // steer the in-flight task
      addChat("user", "💬 " + text);
      try {
        await api("/api/interject", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task: text }),
        });
        addLog("插话", "agent", "已发送提示，agent 将在下一步看到");
      } catch (err) { addChat("system", `插话失败: ${err.message}`); }
      return;
    }
    addChat("user", text);
    try {
      await api("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: text }),
      });
    } catch (err) { addChat("system", `启动失败: ${err.message}`); }
  };
  $("task-input").onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("btn-send").onclick();
  };
  $("btn-stop").onclick = () => api("/api/stop", { method: "POST" });
  $("btn-clear-log").onclick = () => { logList.textContent = ""; };
  $("btn-settings").onclick = () => $("drawer").classList.add("open");
  $("btn-close-drawer").onclick = () => $("drawer").classList.remove("open");
}
main();
