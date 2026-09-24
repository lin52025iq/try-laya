# try-laya

**Laya-first、本地优先、可人工接管的 Reactive Agent Runtime。**

这是第一版可运行工程，不是“导入攻略即可自动玩任意 FPS”的完成品。默认决策后端是真实 Laya SDK；未安装或未预热会停止并报错，**不会偷偷切换规则或 LLM**。显式 `--policy demo` 仅用于测试执行链，不代表模型能力或模型速度。

## 已实现的工作路径

```
用户目标 / 外部攻略 → 结构化 Profile → 人工检查并激活
                                          ↓
Runtime → Observation → 有效候选集 → Laya → 版本/权限/审批检查
   ↑                                             ↓
   └──────── 验证结果 ← Executor / 有期限控制帧 ──┘
```

LLM 是可选的慢路径：把攻略和现有状态绑定编译成策略草稿，不自动激活，不逐步生成动作。无 LLM API Key 也能导入外部生成的 JSON Profile。Laya 选择动作，执行器填入本地任务数据；表单值不发送给 Laya。

| 能力 | 当前实现与边界 |
|---|---|
| Laya | 本地 `Router.predict`；远端 `/v1/systemone`；预热、概率/候选验证、超时拒绝、禁止积压旧推理 |
| Browser | Managed Chromium；DOM 探针、真实点击/填写/选择、画面及人工输入；不是任意网页零配置理解 |
| Attached Chrome | API 支持本机 CDP，在已有 context 中创建一个本服务拥有的新标签；仅 Assist/Manual，不接管用户当前标签 |
| Android | 指定 ADB serial；截图、UI tree、点击、受限 ASCII 输入、按键；未实现多点触控/持续按键 FPS 输入桥 |
| 实时控制 | Browser 短时 ControlFrame、序号、TTL、按键差量更新、失效释放；内置追踪沙盒使用显式数字状态，不是视觉识别游戏 |
| 会话 | Session / Episode、目标/策略/控制版本、模式和状态分离；新 Episode 不会自动重置游戏 |
| 控制与数据 | 单执行出口、一次性审批、人工抢占、SQLite 事件/执行日志、WebSocket、显式保存截图 |
| 控制台 | 中文本地 Web Console，任务/模型预热/画面/接管/审批/事件/外部策略导入 |

本地实际验证范围见 [验证记录](docs/VALIDATION.md)。开发环境没有可用 Laya 权重和 ADB 设备：已验证真实 Chromium + 显式 Demo 的闭环，**未声称真实 Laya 推理、FPS 实战或 Android 真机测试通过**。

## 安装与运行

Python 3.11+。先创建虚拟环境，安装本项目、Laya 和 Chromium。首次预热需要下载上游模型权重；这些文件不会进入本仓库。

### macOS / Linux

```bash
git clone https://github.com/lin52025iq/try-laya.git
cd try-laya
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,laya]'
agentctl browser-install
agentctl doctor
agentctl serve
```

### Windows PowerShell

Windows **不要求安装 `py.exe` / Python Launcher**。有些 Python 安装只有 `python.exe`，此时执行 `py -3.11 ...` 会直接得到 “`py` is not recognized”。推荐使用仓库自带的启动脚本，它会依次寻找 Python Launcher、常见的本机 Python 安装和 PATH 中的 `python.exe`，并且不要求激活 venv：

```powershell
git clone https://github.com/lin52025iq/try-laya.git
cd try-laya

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1

.\.venv\Scripts\agentctl.exe serve
```

只想先验证浏览器/控制链而不安装 Laya/Torch：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -DemoOnly
.\.venv\Scripts\agentctl.exe serve --policy demo
```

手工安装也可以完全不用 `py`：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,laya]"
.\.venv\Scripts\agentctl.exe browser-install
.\.venv\Scripts\agentctl.exe doctor
.\.venv\Scripts\agentctl.exe serve
```

当前 CI 在 Linux 验证 Python 3.11/3.13，并在 Windows 验证 Python 3.12。Python 3.14 可以被启动脚本发现，但目前尚未作为本项目的正式验证版本；如果 Laya/Torch 在 3.14 上安装失败，建议并行安装 Python 3.12：

```powershell
winget install -e --id Python.Python.3.12
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

脚本会直接寻找 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，所以即使安装后仍没有 `py` 命令也能继续。更多排错见 [Windows 安装说明](docs/WINDOWS.md)。

打开终端显示的 `http://127.0.0.1:8787`，粘贴本次启动的 Bearer token，然后连接服务、**在当前服务进程中点击「预热 Laya」**。默认模型为 `multilingual`，默认设备为 CPU；通过环境变量 `LAYA_MODEL` / `LAYA_DEVICE` 配置。

`agentctl warmup` 是一次独立进程的安装/加载检查，退出后不会替另一个服务进程保持模型常驻。不要把它和服务内预热混为一谈。

### 首次浏览器验证

选择 `Local form / 本地表单`，URL 留空，使用内置的无网络 HTML 测试页。任务数据示例：

```json
{"name":"Alex","company":"Example Studio"}
```

默认 Assist 模式，每次动作都需要批准。只在这个受控测试页上验证自动闭环时，选择 Auto 并勾选“允许当前会话自动点击/提交”，再创建会话并运行。应完成填写姓名、填写公司、提交三个动作，页面出现 `Saved`，会话变为 `COMPLETED`。真实模型可能选择等待或请求新上下文；应记录失败并调整 Profile/模型，不能把阈值一路降低后当作可靠性提升。

已有 Chromium 可以通过 `BROWSER_EXECUTABLE` 指定可执行文件；`agentctl serve --headed` 显示浏览器窗口。**自动模式的人工接管保证只覆盖控制台/API 输入，尚未监听操作系统层的直接鼠标键盘输入。** 在外部窗口手工操作前先在控制台点击接管。

### 显式 Demo：先检查安装和执行链

```bash
python -m pip install -e '.[dev]'
agentctl browser-install
agentctl serve --policy demo
```

控制台始终标记 `demo-NOT-LAYA`。它是确定性脚本，不会下载模型。可以验证真实浏览器表单、画面、事件、审批及接管，不能据此报告 Laya 的延迟、准确率或游戏水平。

### 接入已有 Laya HTTP 服务

上游服务需要预加载选定 checkpoint。示例为 macOS/Linux，Windows 设置相同环境变量即可：

```bash
python -m pip install 'laya[serve]==0.3.18'
LAYA_MODELS=multilingual LAYA_PRELOAD=true LAYA_DEVICE=cpu laya-serve
```

另一个终端启动本项目：

```bash
export LAYA_BASE_URL=http://127.0.0.1:8000
export LAYA_MODEL=multilingual
agentctl serve --policy http
```

服务端启用了 `LAYA_API_KEY` 时，客户端也需配置同名变量。HTTP 适配器采用上游 `POST /v1/systemone`，不是 OpenAI chat API。只配置可信地址，不把无鉴权模型端点开放到公网。

## 策略、游戏上下文与 LLM

Profile 集中承载知识摘要、决策条件、状态探针、动作空间和验证条件；它可跨 Episode 复用。导入后先保存，再针对会话显式激活；激活会更新策略版本、废弃旧决策并暂停。

读取 [Profile 格式与导入](docs/PROFILES.md) 和内置 JSON：

- [表单策略](src/laya_runtime/builtin_profiles/form.json)
- [实时追踪策略](src/laya_runtime/builtin_profiles/realtime.json)

内置追踪策略把“向左、向右、停止”都提供给 Laya，由模型结合数字偏移选择；权限检查及 TTL 不是模型决策。选择此策略、Browser URL 留空、Auto 模式并授予短时控制权限，即可运行追踪沙盒。它**没有**真实游戏视觉检测、地图导航或瞄准能力。

使用内置 LLM 草稿编译器时设置 `LLM_BASE_URL`（含 `/v1`）、`LLM_MODEL` 和可选 `LLM_API_KEY`。兼容支持 JSON 输出的 chat-completions 服务。页面中的“生成草稿”只返回 JSON，不执行、不授权、不激活。未配置 LLM 时，直接从外部模型获取符合 schema 的策略包并导入。

**攻略本身不提供像素到状态的识别器，也不提供设备端低延迟输入。** 接入具体 FPS 仍需游戏专用感知和输入桥，然后分别验证 Laya 战术选择、模型响应时间和总闭环时延。

## 测试与验收

```bash
python -m pytest -q
agentctl check-profile src/laya_runtime/builtin_profiles/form.json
agentctl benchmark --policy local --iterations 20 --output laya-benchmark.json
```

最后一条只测一个固定决策样例，**不是**浏览器/游戏端到端性能测试。已安装模型后显式运行真实模型验收：

```bash
RUN_LIVE_LAYA=1 AGENT_POLICY=local python -m pytest -q -m live_laya
TEST_ADB_SERIAL=emulator-5554 python -m pytest -q -m android
RUN_NETWORK_BROWSER=1 python -m pytest -q -k outbound_navigation
```

PowerShell 示例：`$env:RUN_LIVE_LAYA="1"; python -m pytest -q -m live_laya`。没有相应资源时测试会跳过，跳过不是通过。视觉控制台复现：`python scripts/visual_smoke.py --output .data/visual-smoke`；此脚本使用明确标注的进程内 ASGI 测试桥，不代表浏览器 HTTP/WebSocket 网络联调。

## 数据与安全

服务只绑定 loopback，API 和 WebSocket 需要令牌，拒绝外源访问。默认没有自动提交或实时控制权限。用户授予 `allow_auto_click` 后意味着信任该会话的点击/提交效果，**不是系统已能准确识别付款等所有语义风险**。

`.data/service.db` 保存事件和执行准入日志；`.data/profiles` 保存策略；截图仅在用户点击保存时进入 `.data/artifacts`。服务重启不恢复活跃会话，不自动重放已有动作。默认不记录任务值或完整状态；`AGENT_RECORD_TRAINING=1` 才记录训练用状态/候选上下文，网页文字仍可能含隐私，需要自行审查与保留策略。

不要上传 `.env`、`.data`、用户截图、浏览器 profile、模型权重或访问密钥。详见 [安全边界](SECURITY.md)。

## 工程与后续边界

代码保持单进程模块化：`models / profiles / policies / runtime / control / service / storage / api`。完整状态机和不变量见 [架构说明](docs/ARCHITECTURE.md)。

本版尚未实现：任意页面自动发现绑定、通用游戏 CV、Android 持续多点控制桥、桌面 Runtime、MCP/Codex/任意 shell 工具执行、模型下载管理 UI、自动长期记忆、自动策略热更新、微调流水线、多进程 worker。后续扩展应复用同一控制边界，而不是让模型直接获得执行权限。
