# WinPilot

> 专为 **DeepSeek V4 Flash**（纯文本模型）打造的 computer-use 级 Windows 操控 Agent。
> 用多维结构化感知（UIA / Win32 / OCR / OpenCV）替代视觉，再以命令行优先、Action→Verify→Retry 闭环把任务做稳做快。

DeepSeek 不支持视觉，WinPilot 把屏幕转成**结构化文本**让模型"读懂"界面，并优先用命令行完成能命令行完成的事——比截图式 computer-use 更快、更省 token。

## 能力一览

- **多维感知融合**：每个维度可单独开关 + 调优先级，每个元素/日志标注来源（`src=uia/win32/ocr/cv`），便于定位是哪一维在起作用
  - `UIA` 控件树（UWP/WPF/WinForms/Qt，最准，带状态）
  - `Win32` 窗口枚举 / 经典控件 / Explorer COM
  - `OCR` RapidOCR + DirectML GPU 加速（AMD/Intel/NV），限定窗口区域识别文字→屏幕坐标
  - `OpenCV` 图标候选检测 / 模板匹配 / 颜色探针 / 加载稳定检测
  - `VLM`（可选）配置任意 OpenAI 兼容多模态端点即获得真视觉
- **命令行优先**：`run_shell`(PowerShell/cmd，捕获完整输出) + `search_files`(Everything 毫秒级全盘搜索，自动降级 PowerShell)
- **Action→Verify→Retry**：每个动作可带期望条件（文字出现/消失、窗口出现/消失、画面稳定、颜色匹配），自动验证 + 失败重试 + 感知维度升级
- **托盘/后台程序**：`find_app` 区分"没运行/在托盘"，`tray_click` 唤起最小化到托盘的程序（QQ/微信等）
- **运行中插话**：任务执行时随时发提示纠偏（像 Claude Code 一样），agent 在下一步决策前采纳
- **Trace + 离线重放**：每步记 JSONL + 截图，一键导出 `.wps.json` 脚本，离线 0-token 重放（脚本精灵基础）
- **经验记忆**：成功配方按应用存档，下次同类任务注入提示
- **DeepSeek 适配**：剥离 reasoning_content、低温、append-only 历史命中 prefix 缓存、上下文超阈值才折叠、UI 实时费用估算
- **Web UI**：左聊天右执行日志（每步维度/验证/耗时）+ 设置面板（维度开关与优先级、模型/VLM、shell 开关、以管理员重启、OCR provider、token/费用）

## 环境要求

- Windows 10/11，Python 3.10+
- 可选：[Everything](https://www.voidtools.com/) 装好并运行（高速文件搜索）；GPU 加速 OCR 需 `onnxruntime-directml`

## 快速开始

```powershell
git clone <this-repo> WinPilot
cd WinPilot
pip install -r requirements.txt

# 配置：复制模板并填入你的 DeepSeek API key
copy config.example.json config.json
# 编辑 config.json，把 api.api_key 改成你的 key

python run.py        # 启动后自动打开 http://127.0.0.1:8765
```

在网页里输入任务即可，例如：
- `打开记事本，输入"你好世界"，另存为到桌面 test.txt`
- `用计算器算 12 × 34，把结果告诉我`
- `随机播放我音乐文件夹里的一首歌`
- `用微信给"文件传输助手"发一条消息：测试`

## 高速搜索（Everything）

`search_files` 优先用 Everything 的 `es.exe`（毫秒级全盘）。若未安装，会自动降级到 PowerShell 遍历（较慢）。
若 Everything 以**服务模式**运行（Session 0），WinPilot 会自动拉起用户会话客户端以打通 IPC。
可在 `config.json` 的 `search.everything_es_path` 指定 es.exe 路径，或放到 `winpilot/bin/es.exe`。

## 项目结构

```
run.py                     入口（启动 server + 开浏览器）
config.example.json        配置模板（复制为 config.json 后填 key）
winpilot/
  config.py                配置/开关单例
  server.py                FastAPI：REST + WebSocket + 静态 UI
  agent/   loop / tools / prompts        Agent 循环、工具派发、系统提示词
  perception/ uia win32 ocr vision_cv vlm tray search fusion model
  action/  executor verify shell         SendInput、验证重试、命令行
  recorder/trace.py        trace 记录 / 导出 / 离线重放
  memory/experience.py     经验记忆
  utils/   log screenshot
webui/                     index.html / app.js / style.css（无构建步骤）
tests/                     冒烟测试
```

## 安全说明

- `config.json` 含 API key，已被 `.gitignore` 排除——**不要提交**
- WinPilot 默认允许执行 shell 命令（无需逐条确认）。在设置面板可关闭 `允许命令行` 强制纯 GUI 模式
- "以管理员重启" 会请求 UAC 提权，提权后可操控管理员窗口、写系统目录
- 服务仅监听 `127.0.0.1`，CORS 限定本机来源

## License

私人项目，保留所有权利。
