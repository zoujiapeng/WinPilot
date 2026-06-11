# WinPilot 开发进度（交接文档）

> 架构计划: `C:\Users\zou\.claude\plans\parsed-stirring-allen.md` + 续作计划 `purrfect-puzzling-nova.md`
> 任务: 为 deepseek-v4-flash 打造 computer-use 级 Windows 操控 Agent（多维感知替代视觉）

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
