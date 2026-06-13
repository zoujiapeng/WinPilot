# WinPilot 开发进度（交接文档）

> 架构计划: `parsed-stirring-allen.md` + `purrfect-puzzling-nova.md` + `shimmying-plotting-biscuit.md`
> 任务: 为 deepseek-v4-flash 打造 computer-use 级 Windows 操控 Agent（多维感知替代视觉）

## 第4轮（2026-06-13）：感知提速 + 不夺光标模式 + 排序鲁棒性

源于用户实测 Krita「慢+步16卡2分钟」+ 两个新需求。

### 感知提速（Krita 卡顿根因修复）
- 实测根因:**OCR 在大窗口 6.4s/次**(冗余,UIA 已给48元素)+ **步16 click 带 screen_stable 验证在画布上永不稳定→132s 卡死**。
- 修1:`fusion` **UIA 元素≥阈值(默认20)时自动跳过 OCR**(`fusion.ocr_skip_when_uia_rich`/`uia_rich_threshold`)。实测记事本 observe 0.68s(OCR skip)。
- 修2:`verify.run_with_verify` 加**单动作总时长硬上限**(timeout×(retries+1)+4s),screen_stable 也受 verify deadline 约束→一个动作再不会卡几分钟。

### 感知维度全开+任意排序 鲁棒性（用户问）
- 实测:4 种排序 + 全5维度(含MiMo)全开,**均不崩、结果正确**。每维独立、异常隔离、merge/dedup 顺序无关。OCR-skip 仅在 UIA 排 OCR 前触发(纯性能,默认满足)。全开+VLM 每步 +11s(MiMo 图像调用,故 VLM 宜按需不宜常开)。

### 🧪 不夺光标模式（实验,默认关 `agent.no_mouse_steal`）
- Windows 单系统光标→真"双鼠标"不可行;实现"不抢你鼠标":点击/滚动走 **PostMessage 后台消息(物理光标不动)**,拖拽/兜底 SendInput 则**做完瞬间还原光标**。元素点击本就走 UIA invoke(不动光标)。
- 实测:开启后点击物理光标 0px 位移;关闭照常(对照)。UI 设置抽屉有开关。
- 注:仅管鼠标;键盘输入仍会抢焦点(用户只要求别抢鼠标)。后台消息点击对个别 app(游戏/部分Chromium)可能无效,会回退 SendInput+还原。
- **13 smoke 全绿**(新增 smoke_no_steal)。

### ⚠️ 待办
- 轮换 DeepSeek+MiMo key。
- 新开关:`fusion.ocr_skip_when_uia_rich`(默认开)、`agent.no_mouse_steal`(默认关,UI可切)。

---

## → Codex 级 第3轮（2026-06-13 夜，无人值守自主完成）：#5 安全 + #4 可逆 + #1 模型代差 + 最终e2e

路线图剩余三项一次性补齐（用户睡觉,授权自主完成+自测+修复）:

### #5 安全/审批层（新增 `action/safety.py`）
- 两级:CATASTROPHIC(format/Format-Volume/关机重启/diskpart/bcdedit/vssadmin delete/**递归删系统或用户根目录**/**PowerShell -EncodedCommand 一律拒绝**)永远拦;DANGEROUS(递归/强制删、del /f/s/q、reg delete、批量taskkill、iex下载执行)按 `agent.approve_mode`(off/guard/dry_run)。
- 接入 shell.run + run_detached。**无人值守跑真机命令时的灾难性熔断**。

### #4 可逆/checkpoint（轻量,诚实）
- `ToolSession._undo_stack` 记 launch(窗口hwnd)/run_detached(pid);新工具 `undo_last`:取消最近后台/定时任务、关**记录的那个**窗口(非当前前台,防关错)、或 Ctrl+Z。GUI 可逆性本就有限。

### #1 模型代差缓解（escalation 路由）
- `agent.escalation_model`(默认空)。卡死结构化replan时下一步 + done-gate judge 用更强模型(deepseek-v4-pro)做这一个决策,随即回 flash。

### 最终多场景端到端（真机自测）
- **eval full 7/7 = 100%**(shell_file/file_write/launch_calc/timer/git_clone/calc_compute/notepad_write),均步2.4 ¥0.128。全部 ground-truth 独立验证,含硬核 GUI(点按钮算408、记事本存盘)。**复用可见省步**(calc_compute 0步=命中复用)。full baseline 已存(按任务集签名分别存)。
- 额外复合场景(建夹+记事本存子目录)done,截屏+文件双验证通过。
- **eval `reuse` 移出评分套件**:它依赖"run1稳定导脚本+run2弱模型选择调reuse"双重随机,是模型脾气不是任务成功;复用机制改由确定性单元测试 `smoke_reuse.py` 守护(直接喂脚本调reuse_skill,0-token重放真建文件,过)。

### 审查与回归
- code-reviewer:**2 CRITICAL**(EncodedCommand绕过灾难拦截→已补一律拒绝;run_detached同源已覆盖)+ 4 HIGH(screen_stable逃逸验证预算、undo关错窗口、taskkill超时、escalation空配置告警)+ 5 MEDIUM + 3 LOW,**关键全修**。
- 期间 eval 驱动出一个重要修复:**done-gate judge 通过即算 verified_run**(不再只认动作级expect)→ 技能脚本导出常态化。
- **12 smoke 全绿**:world/memory/session/perception/detached/aliases/groundtruth/safety/reuse/eval/agent/verify。

### 路线图状态:✅ 全部完成(#1-#11 差距项 + 6 个改进轮)
- 留作可选增强(非必须):eval full 多刷几次取均值降噪;done-gate judge 也吃 ground-truth 进一步减空耗;让弱模型更主动用 reuse_skill(prompt 调优,收益有限)。

### ⚠️ 待办
- **轮换 DeepSeek + MiMo 两个 key**(整轮对话明文出现过)。
- 配置项:`agent.approve_mode`(默认off,要更严可设guard)、`agent.escalation_model`(留空=不升级,配 deepseek-v4-pro 可启用)。

---

## → Codex 级 第2轮（2026-06-13）：#6 规划深度 + done-gate 证据修复

### #6 规划深度
- **子目标验证门**:planner 可给子目标挂 `check` expect(file_exists/window_appears 等),`complete_subgoal` 调 `verify.check` 核验,未过则拒绝标完成(不许跳步)。无 check 的子目标照常。畸形/未知 check 键在解析期被丢弃(防永久阻塞)。
- **结构化 replan**:`world.take_stuck_signal()` 边沿触发(每卡死 episode 只报一次,**仅真实界面变化才重置**,防 replan 连刷),loop 注入"卡在哪/试过什么/换什么新方法+revise_plan"结构化反思,替代旧的每步刷 advisory。
- 向后兼容:纯字符串 plan、revise_plan 传字符串数组照常工作。

### done-gate 证据修复(eval 驱动出来的关键改进)
- 问题:eval 实测 #6 后 shell_file 从 4 步→9 步且 agent=fail——root cause 是 **done-gate 的 judge 只看 GUI 屏,看不到桌面文件夹就瞎拒**,agent 徒劳开资源管理器找文件夹空耗。
- 修复:`_handle_tool_calls` 把 run_shell/run_detached/verify 等**工具真实输出存为证据**喂给 done-gate judge,并指示"文件/进程类以确定性证据(Test-Path/Exists/verified)为准,GUI 看不到不算未完成"。
- **eval 实测改进(优于原 baseline)**:成功率 100% 不变,均步 5.0→**1.3**,花费 ¥0.071→**¥0.018**,72s→41s。shell_file 9步空耗→2步。**新 baseline 已存**。

### 这一轮完整演示了 eval 闭环的价值
发现(均步↑/agent=fail)→ eval 量化 → root cause(done-gate 忽略证据)→ 修复 → 再 eval(反超 baseline)。**这就是"别负优化"从口号变成可验证的机制。**

### 审查与回归
- code-reviewer:0 CRITICAL,2 HIGH(子目标门 screen_stable 逃逸 4s 预算→现受 verify deadline 约束;结构化 replan 进度条场景连刷→仅真实界面变化重置)+ 3 MEDIUM(证据加 run_detached/放宽截断、畸形 check 解析期丢弃、…)+ 2 LOW 全修。
- **10 smoke 全绿**:world/memory/session/perception/detached/aliases/groundtruth/eval/agent/verify。

### 后续轮次(每轮 eval 度量)
- **#4 可逆/checkpoint**、**#5 安全/审批**、**#1 模型代差**(卡死/judge 升 deepseek-v4-pro)。建议某轮跑一次 `eval full`(含 notepad/calc/clone GUI 任务)给复杂任务也立 baseline。

### ⚠️ 待办
- 轮换 DeepSeek+MiMo key(对话明文出现过)。

---

## → Codex 级 第1轮（2026-06-13）：eval 闭环(#3) + 验证大修(#2)

差距分析后用户要求全面升 Codex 级、分阶段、特别是 #2/#3。本轮交付:

### #3 eval 闭环 — 新增 `winpilot/eval/`
- `tasks.py`:`EvalTask{id,prompt,check(ctx)->(ok,detail),setup,cleanup,tags,max_steps,repeat}`,`SUITE` 8 任务(shell_file/file_write/launch_calc/timer/git_clone/calc_compute/notepad_write/reuse)。**检查器全是程序化 ground-truth(查文件/进程/回复文本),绝不信 agent 自述**。suite=smoke(3便宜)/full。
- `runner.py`:in-process 跑 `AgentRunner`(真API真GUI)→ 采集 status/steps/cost/tokens/elapsed → check() → 计分卡 `eval/results/<ts>.json` + baseline 对比(成功率/均步/花费 delta)。`__main__`:`python -m winpilot.eval [smoke|full] [--tasks ..] [--save-baseline]`。
- **首次 baseline 已立(smoke 3/3=100%, 均2.7步, ¥0.071, 72s)** → `eval/baseline.json`。意义:从此"改动是优化还是负优化"有数可查。

### #2 验证大修
- **确定性 ground-truth** — 新增 `perception/groundtruth.py`(file_exists/file_contains/process_running[精确优先]/process_gone/shell_true[deadline绑定]),接入 `verify.py` expect DSL 新键。**GUI 任务触及文件/进程的部分用确定性验证代替像素启发式**(Codex 级 ground truth)。eval 检查器复用同模块。
- **done-gate(杀假done核心)** — `loop._verify_done`:agent 调 done 时先重新 observe + 独立 skeptical judge-LLM(只输出JSON{done,reason})核验;judge=false 注入"未通过请继续"回退不终止,最多拒 `done_max_rejects`(默认2)次/或剩余步数≤2 时放行但标 `_done_unverified`(使 verified_run=False,不进可信技能库)。**fail-open**(judge异常/解析失败=放行,绝不死锁)。`agent.verify_done` 默认 on。
- prompts:补 ground-truth expect 文档 + "done 会被独立核验,别糊弄;涉及文件/进程先用确定性 expect 自证"。

### 真机自证(eval smoke 日志实拍)
- shell_file:done 被拦1次("界面没显示文件夹")→ 补验证 → ground-truth 确认文件夹真建 ✓
- timer:done 连拦2次("没验证8秒后计算器真开")→ 达上限标未验证 → ground-truth 等到点确认计算器真出现 ✓
- **假done 三重防护**:eval ground-truth 判负(smoke_eval fakedone_task: agent=done→pass=FAIL) + done-gate 拦截 + 不可信不进技能库。

### 审查与回归
- code-reviewer:**1 CRITICAL 已修**(loop 漏导入 fusion → done-gate 在 GUI 路径 NameError 被 fail-open 静默吞掉;smoke 全 shell 任务没暴露,正是 review 价值)+ 2 HIGH(done-gate 步数预算短路、eval 用 CONFIG.set 替直改 _data + setup 异常隔离)+ MEDIUM(process 精确匹配优先、file_exists 单次、shell_true deadline 绑定)全修。
- **10 smoke 全绿**:world/memory/session/perception/detached/aliases/groundtruth/eval/agent/verify。

### ⚠️ 待办/已知
- 轮换 DeepSeek+MiMo key(对话明文出现过)。
- done-gate 对"确定性基底"任务(建文件夹/定时)偏激进(judge 看 GUI 屏看不到结果→易拒,靠 ground-truth 终判兜底);后续可让 judge 也吃 ground-truth 信号,减少空耗。
- 后续轮次(每轮用 eval 度量):#6 规划深度(子目标验证门)、#4 可逆/checkpoint、#5 安全/审批、#1 模型代差(卡死/judge 升 deepseek-v4-pro)。

---

## 记忆升级（2026-06-12 晚）：会话连续性 + 技能自提炼与复用

用户反馈"说'再来一次'像重新开始对话""记忆该自主提炼起作用的部分、像复用脚本一样复用"。三处落地（混合复用，已确认）：

### A. 会话连续性（修"再来一次=重开对话"）— 新增 `agent/session.py`
- 根因：每任务都是独立 `AgentRunner`，`messages=[system,task]`，上个任务全丢，指代指令无锚点。
- `SESSION` 进程级单例（重启=新会话）存最近任务；`server.py` worker 跑完 append，`/api/chat` 把 `recent()` 传入 runner；`loop.run` 注入"【本会话此前任务】…若指令含'再来一次/刚才那个'等指代,指最后一条"到历史尾部。`agent.session_memory` 默认 true；`/api/session` GET/DELETE + UI 显示。
- **真机验证 ✓**：「打开计算器」→关→「再来一次」→ agent 答"好的,再来一次——打开计算器!"并真的重开。

### B. 技能记忆自提炼（存"实际起作用、可泛化"的）
- `recorder/trace.clean_selector_text` 剥离易变 token（"消息: 132"→"计技25A葛瑞念"、尾部 (5)、未读数）；`_distill_steps` 相邻重复去重。
- `experience._load` 迁移：清理旧格式垃圾步骤（含 `element_id`/`hwnd` 的 `name({json})` 串），清空则淘汰配方。recipe 增 `script` 字段。

### C. 复用像脚本（混合）— 打通经验与重放引擎
- `loop.run` 在 done+verified 时自动 `export_script(run_id)` → 回写 `recipe.script`。每个可信成功=可重放 .wps.json。
- `hints_with_ids`：强匹配（归一化包含/bigram≥0.7，≥3字）且有 script → 升级为"⭐上次成功流程(可复用),可调 reuse_skill"。
- 新工具 `reuse_skill(task/recipe_id)`：跑 `recorder.replay`（0-token,文字→坐标重定位），**失败步告知位置+回退实时**（混合,不硬依赖）。stop_event 已串入 ToolSession 可中断。
- **真机验证 ✓**：reuse_skill 重放计算器,verify 失败时正确回退报"从第1步实时操作"。

### 审查与回归
- code-reviewer：0 CRIT/HIGH，2 MEDIUM 修（reuse_skill 不可中断→stop_event 串入；_strong_match 短串误匹配→≥3字守卫）+ OSError 升 warning。Finding-1(SESSION race)被 409 busy-lock 挡住,非问题。
- 8 smoke 全绿：world/memory/session/perception/detached/agent/verify/search（新增 smoke_session，扩展 smoke_memory）

### 已知小瑕疵（不影响功能，未修）
- ~~Win11 计算器窗口标题是英文 "Calculator"，`expect window_appears:"计算器"` 会重试到超时再 list_windows 兜底成功~~ **已修(2026-06-12晚)**：新增 `perception/app_aliases.py` 中英应用名别名表(计算器↔Calculator/设置↔Settings/资源管理器↔Explorer 等20+组)，`win32.find_window` 别名感知(verify 的 window_appears/gone 自动受益)。真机验证:"打开计算器"现在 verify 通过并导出可复用脚本。新增 `tests/smoke_aliases.py`(9 smoke 全绿)。

---

## 真机验收（2026-06-12 下午）：4 任务测试 + 暴露问题的结构性修复

用 DeepSeek 真机跑 4 个用户任务，验收地基层并修复暴露的结构性缺口（非偶发，是能力缺失）。

| 任务 | 结果 | 说明 |
|---|---|---|
| 1 开发贪吃蛇+实时玩 | ⚠️ 部分 | 文件创建/Chrome打开 OK；**实时玩 canvas 游戏=已知前沿限制**（纯文本模型对像素画面天然瞎）。已结构性修复"盲而不自知" |
| 2 QQ给葛瑞念打电话+发消息 | ✅ done | 规划todo+find_app+launch解析lnk+UIA定位通话/挂断/发送全链路真机通过，截图确认"我是ai"已发、通话"已取消" |
| 3 定时提醒吃饭+开计算器 | ✅ done | 新增 run_detached 后**端到端真实按点触发**（计算器在预期时刻±3s自动出现，msg提醒弹出） |
| 4 克隆开源项目并运行 | ✅ done | 自主选 octocat/Hello-World，git clone(走代理)+查看+写show.py运行，桌面真实仓库为证 |

### 暴露问题 → 结构性修复（不是继续打 prompt 补丁）
1. **网易云音乐找不到**（用户报告）→ 新增 `perception/apps.py` 已安装应用解析器（开始菜单.lnk + Get-StartApps[强制UTF-8,Store应用如网易云仅有AppID] + App Paths注册表 + Everything兜底）+ `find_program` 工具 + launch 对中文/显示名自动解析。端到端验证网易云真启动。
2. **缺可靠定时/后台原语**（任务3三跑三败：Start-Job随宿主死/EncodedCommand引号地狱/模态MessageBox/残留进程乱触发）→ 新增 `shell.run_detached`（写.ps1→Start-Process -PassThru -File 拉起独立存活进程，返回PID可验证，零引号问题，自动清理旧脚本）+ `run_detached` 工具。彻底解决整类。
3. **canvas/游戏感知盲区且不自知**（任务1）→ `fusion.Snapshot.to_text` 对"大窗口+结构元素<3"显式标注"⚠️结构化感知几乎为空，用vlm_describe，勿盲猜" + prompt 同步引导。
4. **shell 中文输出乱码掩盖失败**（任务1空文件被信）→ `shell.run` 改 UTF-8/GBK 双解码 + PowerShell 强制 UTF-8 输出。
5. **空文件却信回显** → prompt "写后必验大小/开头；here-string 必须 `$var|Out-File`"。
6. **开放式任务停下来反问**（任务4首跑）→ prompt "无人值守，'一个/随便/简单的'等开放措辞自己合理选择执行，不反问"。
7. **PowerShell 不支持 `&&`**（任务4浪费2步）→ prompt 明确用 `;` 或多次 run_shell。

### MiMo grounding 实测（Tier3-G 配套）
- base_url=`https://api.xiaomimimo.com/v1`，视觉模型 `mimo-v2.5`/`mimo-v2-omni`（flash/pro无视觉）
- **MiMo=Qwen2.5-VL系 norm-1000 归一化坐标**（按像素读差170px，解码后5.7px、8/8命中，门禁PASS）；**是推理模型，须 `thinking disabled` 否则吃光token空回复**

### 回归
- 7 个 smoke 全绿：world/memory/perception/detached/agent/verify/search
- code-reviewer 审 run_detached：1 HIGH（路径含单引号注入/失败）+ 3 MEDIUM（文件累积/错误回传/write异常）全修
- 残留测试进程已清；Hello-World 仓库留在桌面（任务4产物，可删）

### 新配置/工具一览（默认值）
工具新增：`run_detached`/`find_program`/`capture_template`/`revise_plan`/`complete_subgoal`/`remember`/`forget`
开关：`agent.world_model/replan_on_fail/planning_enabled/playbooks_enabled`、`fusion.escalate_on_empty`、`vlm.thinking` 均默认就绪

### ⚠️ 待办/风险
- **轮换 DeepSeek + MiMo 两个 key**（均在对话明文出现过；MiMo key 现在 config.json 的 vlm 段）
- 实时游戏/canvas 操控对纯文本模型仍是前沿限制（配 VLM 视觉主导模式可缓解，未深测）

---

## 阶段三（2026-06-12）：地基层（规划/世界模型/恢复）+ 可靠性 + VLM grounding

源于 11 项差距分析（vs Codex/Claude Code/OpenHands/Manus），落地 Tier1+2+3，排除两项负优化（自动唤醒续跑、重型多agent）+ 主动否决 Top-K 工具检索（18-20 工具全量 schema 吃缓存红利，动态列表反而打爆 DeepSeek 前缀缓存）。

### Tier1 地基层
- **`perception/world.py` 世界模型**：跨步追踪动作+快照指纹（结构性变化），卡死检测（连续3次同动作 / 连续3次动作后无变化）→ 只在有价值时向历史尾部注入一行 advisory（平时安静省token）。`agent.world_model` 开关。wait→observe 轮询不计入无变化（不误判）
- **宏观恢复**：`fail()` 软化两段式（首次拦截强制换思路，revise_plan 后重置配额）；`agent.replan_on_fail` 开关
- **`agent/planner.py` 规划层**：≥45字或时序词≥4次才上 front 规划（计算器类零开销）；`revise_plan`/`complete_subgoal` 工具；计划注入稳定前缀一次+进度走工具结果（不破缓存）；UI 显示 todo。`agent.planning_enabled` 开关

### Tier2 可靠性
- **fusion 空快照升级链**：全维度瞎了→低conf窗口OCR(0.4)→全屏OCR→VLM，绝不给模型空屏幕。`fusion.escalate_on_empty` 开关（维度隔离测试时关）
- **经验记忆全重写**：recipe 带 id/uses/successes/provisional；**模型自称done但无外部verify→存为provisional**（防假经验）；注入后成败回写评分，3次注入全败自动淘汰；步骤改意图级（`click "保存(S)"` 而非坐标）跨分辨率可用
- **记忆CRUD**：`POST/PUT/DELETE /api/memory(/{id})` + UI 列表增删改 + agent 工具 `remember`/`forget`（口头"记住/忘掉"）
- **模板系统复活**：templates_img 原是空的（find_image 死功能）→ `capture_template` 工具自举积累 + README；刻意不预置盲拍模板（错模板=负优化）

### Tier3 VLM + 泛化
- **MiMo 实测**（key在config.json vlm段）：base_url=`https://api.xiaomimimo.com/v1`；视觉模型=`mimo-v2.5`/`mimo-v2-omni`（flash/pro 无视觉）
- **关键发现：MiMo 是 Qwen2.5-VL 系 norm-1000 坐标**！按像素解读误差170px+，按 norm-1000 解码→**平均误差 5.7px，8/8 点击命中，门禁 PASS**（`tests/bench_vlm_grounding.py` 可复跑）
- **MiMo 是推理模型**：reasoning 会吃光 max_tokens 导致空回复 → `extra_body={"thinking":{"type":"disabled"}}` 关闭（精度反而更好），`vlm.thinking` 开关
- vlm.py 修死16×16框→真bbox；真屏 collect 42元素全真框26.9s（定位=兜底维度/视觉主导，非每步跑）
- **视觉主导模式 = 现有机制**：UI 开 VLM 维度+优先级设1 即是，无新代码
- **playbook 数据层**：文件对话框/托盘/播媒体三段技巧迁出 prompts.py → `playbooks/*.json`（关键词匹配注入 + **运行时窗口标题触发兜底**，最长触发词优先）；基础prompt 3131→2033字；`agent.playbooks_enabled` 关=全量注入等价旧行为；加新app知识=加JSON不改代码

### 审查与回归
- code-reviewer：0 CRITICAL，4 HIGH 全修（PUT exclude_unset / playbooks mtime缓存 / save_template截断+空名报错 / experience id去重）+ MEDIUM/LOW 数项；2条误报已核实（wait轮询不误判、_decode_box正确）
- 新增测试：smoke_world / smoke_memory / smoke_perception / bench_vlm_grounding；全部6个smoke绿
- **待办：真机 DeepSeek 回归（场景A/B + 多步长任务验证规划 + 人为卡死验证恢复）——花钱+接管桌面，等用户拍板**
- ⚠️ MiMo key 在对话中明文出现过，**建议轮换**（DeepSeek key 同样老问题）

### 新配置开关（默认值）
`agent.world_model=true` `agent.replan_on_fail=true` `agent.planning_enabled=true` `agent.playbooks_enabled=true` `fusion.escalate_on_empty=true` `vlm.thinking=false`

---

## 阶段二（2026-06-11）：命令行优先 + 高速搜索 + 高权限 + 缓存优化

实测随机播歌失败暴露：agent 看不到命令输出（launch 是 fire-and-forget），只能在 GUI 硬绕。新增：
- **`action/shell.py`** `run_shell` 工具：subprocess.run 捕获 stdout/stderr/exit_code，UTF-8，超时；BUS 推日志。`config.agent.shell_enabled` 关掉=强制纯 GUI 模式（已验证关掉后 run_shell 不出现且被拒）
- **`perception/search.py`** `search_files` 工具：Everything es.exe 秒级（已下载官方 es.exe 到 winpilot/bin/），PowerShell 降级兜底。
  - **会话隔离坑**：本机 Everything 以「服务」模式跑在 Session 0，es.exe 在用户会话找不到 IPC 窗口（Error 8，与提权无关）。
  - **自愈方案**：search.py 检测到 Error 8 时自动 `Everything.exe -startup` 拉起用户会话客户端（连服务、暴露 IPC），重试一次。已验证生效：Vintersaga 150ms、全盘 *.exe 91ms（对比 PowerShell 扫单目录 45s 超时）
  - 实时服务器 search_files 确认走 everything 引擎
- **prompts 命令行优先原则**：文件/启动/播放/系统查询优先 shell；仅「明确要求用界面」或「本质可视交互」才走 GUI
- **loop 缓存优化**：折叠改为「上下文 >48K 字符才触发」，短任务零历史改写→prefix 缓存最大化（旧逻辑每折叠一次打断一次缓存）
- **费用估算**：_track_usage 按 DeepSeek 三档计价（cache-miss 2/cache-hit 0.2/output 8 元每百万），UI tokens 徽章显示 ¥X
- **高权限**：`/api/elevate` ShellExecute runas 重启弹一次 UAC；/api/status 返回 is_admin；UI 权限徽章+「以管理员重启」按钮+shell 开关

### 播歌修复关键（迭代3次）
- 真因：`Start-Process 歌曲文件` 走关联，WMP /Open 不留进程=假播放；agent 还只信回显"已启动"就报 done
- 正解：`Start-Process "wmplayer.exe全路径" -ArgumentList '"歌曲"'`（直调 exe、非阻塞、常驻）+ run_shell 查 Get-Process wmplayer 自验证
- 坑：`& "wmplayer.exe" 歌曲` 用 call 操作符会阻塞→run_shell 超时杀掉播放器；务必用 Start-Process
- **改 prompts.py 必须重启服务器**（system_prompt 是已导入模块的函数，编辑文件不热加载）
- 验证：第4次运行 wmplayer 进程真常驻，歌真在放 ✓；文件操作组（建夹+写文件+读回）✓

### 阶段二验收
| 验证 | 结果 |
|---|---|
| 随机播歌（shell 路径） | ✓ wmplayer 常驻真播放，¥0.026 |
| 文件操作（建夹/写/读回） | ✓ 桌面文件内容正确 |
| 强制 GUI 模式（shell_enabled=false） | ✓ run_shell 消失且被拒，search_files 保留 |
| 缓存/费用 UI 显示 | ✓ tokens+¥ 实时；短任务 cache 74-83% |

---

## 状态: ✅ 全部完成（2026-06-11），五个端到端真机场景验收通过

| 场景 | 配置 | 结果 |
|---|---|---|
| A 记事本另存为桌面 | **关 UIA+Win32，纯 OCR+CV** | done，文件内容精确正确，81k tokens (cache 66k) |
| B 计算器 12×34 | UIA 全开 | done，全程 uia_invoke 快路径，正确读回 408，56k tokens |
| C 离线重放 | 导出 A 的 .wps.json | 0 token 重放成功，文件重建 |
| D 第三方 Notepad++ | 全维度 | done，Scintilla 编辑区输入+另存为成功 |
| E QQ NT 发消息给"我的手机" | 全维度+托盘能力 | done，find_app→tray_click唤起→搜索→发送，消息独立验证在聊天记录 |

## 场景 E 暴露并补齐的能力（重要）
- 首跑 fail：QQ 已登录驻留托盘，list_windows 看不到；agent 重复 launch QQ.exe 弹出**新账号登录窗口**打转40步
- **新增 `perception/tray.py`**：list_tray_icons（Shell_TrayWnd UIA 遍历 + 自动展开 Win11 溢出区"显示隐藏的图标"）+ click_tray_icon；XAML 任务栏 UIA 树懒加载需空结果重试
- **新增 `win32.find_app(name)`**：Toolhelp 进程快照 + 全量窗口枚举（含隐藏），返回 running/has_visible_window，agent 可区分"没运行"vs"在托盘"
- prompts 加"托盘/后台程序唤起法"：launch 前必须 find_app；禁止重复启动 IM 软件
- 实测亮点: QQ NT (Chromium) 的 accessibility 被 UIA 查询激活后元素丰富（84个），uia_set_value 搜索框输入、发送按钮 expect text_gone 验证均成功

## 关键迭代教训
- 场景 A 首跑 fail：OCR 模式下另存为对话框文件夹树导航迷路（40步耗尽，382k tokens）
- **修复**: prompts.py 加"文件对话框必胜法"（点文件名框→ctrl+a→输完整绝对路径→enter→expect window_gone）+ 注入本机桌面/文档/下载实际路径 → 复跑 done 且 token 降到 81k
- code-reviewer 审查出 6 HIGH 全修：路径遍历×2（server relative_to 校验）、CORS 限定 origin、trace 锁内截图 I/O 改三段式、experience 原子写(os.replace)、verify 动作异常后不验证旧屏幕（防假阳性）；2 MEDIUM 也修（color_match 缺 rgb 报错、config.reload 锁内读）

## 文件全景（C:\Users\zou\WinPilot\）
- run.py（uvicorn+开浏览器）/ config.json（API key 在此）/ webui/ 三件套
- winpilot/: config.py, server.py(REST+WS+CORS+路径校验)
  - perception/: model uia win32 ocr(DML注入) vision_cv vlm fusion
  - action/: executor(SendInput+UIA快路径), verify(expect DSL+感知升级+run_with_verify)
  - agent/: prompts(文件对话框策略+路径注入), tools(15工具+replay元数据), loop(reasoning剥离+observe折叠保留2+JSON自纠+usage/cache)
  - recorder/trace.py(TraceRecorder+export .wps.json+Replayer文字重定位), memory/experience.py(原子写配方)
- tests/: smoke_verify.py, smoke_agent.py（均过，无 API 调用）

## 运行方式
`cd C:\Users\zou\WinPilot; $env:PYTHONIOENCODING='utf-8'; python run.py` → http://127.0.0.1:8765
UI: 左聊天右日志，设置抽屉里维度开关+优先级/经验记忆/dry-run/模型/VLM/OCR provider 显示

## 已验证事实
- deepseek 历史回传须剥离 reasoning_content；prompt cache 命中率 80%+（折叠策略下）
- OCR 实跑 DmlExecutionProvider；UIA 快路径 uia_invoke 每步 verified
- 经典记事本/Notepad++ 的另存为都是 Win32 #32770 对话框，完整路径法通杀
- 测试一律 PYTHONIOENCODING=utf-8；服务器后台任务需 TaskStop 再重启生效

## 待办（可选增强）
- 用户已暴露 API key 于对话，**提醒轮换**（已提醒）
- VLM 模式（配 Qwen2.5-VL llama-server 即启用 vlm_describe + vlm collect）未真机测
- 经验记忆命中注入已实现，长期效果待观察
