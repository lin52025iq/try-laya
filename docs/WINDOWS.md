# Windows 安装与排错

本项目不依赖 Windows 的 `py.exe`（Python Launcher）。`py` 只是 Windows 上一种可选的 Python 启动方式；只安装了 `python.exe` 时，`py -3.11 ...` 会报 “The term 'py' is not recognized”。

## 推荐安装

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

脚本会：

1. 优先寻找 Python 3.13/3.12/3.11；
2. 支持存在或不存在 `py.exe` 两种情况；
3. 搜索 `%LOCALAPPDATA%\Programs\Python\Python3xx\python.exe`、`%ProgramFiles%\Python3xx\python.exe` 和 PATH；
4. 创建 `.venv`；
5. 使用 venv 内的 `python.exe` 安装项目和 Laya；
6. 安装 Playwright Chromium；
7. 运行 `agentctl doctor`。

完成后直接运行：

```powershell
.\.venv\Scripts\agentctl.exe serve
```

不需要 `Activate.ps1`，因此不会受常见的 PowerShell venv 激活执行策略问题影响。

## 只验证程序链路

暂时不安装 Laya/Torch：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -DemoOnly
.\.venv\Scripts\agentctl.exe serve --policy demo
```

Demo 只能验证浏览器、Session、控制和审批链，不能当作 Laya 模型验收。

## `py` 不存在

这是正常情况。先检查：

```powershell
python --version
Get-Command python
```

如果 `python` 可用，可以手工执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,laya]"
.\.venv\Scripts\agentctl.exe browser-install
.\.venv\Scripts\agentctl.exe doctor
```

## 只有 Python 3.14

当前项目在 Linux CI 验证 3.11/3.13，并在 Windows CI 验证 3.12。3.14 不是当前发布验收矩阵的一部分；启动脚本不会因为 3.14 而直接拒绝，但如果 PyTorch/Laya 的 Windows wheel 在当前机器上不可用，建议安装并行的 Python 3.12：

```powershell
winget install -e --id Python.Python.3.12
```

安装后无需 `py` 命令，重新执行 `setup_windows.ps1` 即可；脚本会直接寻找：

```text
%LOCALAPPDATA%\Programs\Python\Python312\python.exe
```

不要删除现有 3.14，Windows 可以并行安装多个 Python。

## `python` 打开 Microsoft Store

Windows 的 App execution aliases 可能把 `python.exe` 指向 Store。可以直接安装 Python 3.12，然后使用本仓库脚本；脚本优先检查真实的 LocalAppData 安装路径。

也可以在 Windows Settings 中搜索 “App execution aliases”，关闭 Store 的 `python.exe` / `python3.exe` alias。

## PowerShell 禁止执行脚本

无需修改系统全局策略，使用单次进程级绕过：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

本命令只对新启动的这一次 PowerShell 进程生效。

## Chromium 安装失败

先确认基础环境完成，再单独运行：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

如果公司网络或代理阻止 Playwright 下载，可以设置 `BROWSER_EXECUTABLE` 指向已有 Chromium/Chrome-compatible 可执行文件，再运行 `agentctl doctor` 检查配置。

## Laya 安装或预热失败

先区分两个阶段：

- `pip install ...` 失败：Python/torch/wheel/网络安装问题；
- 控制台“预热 Laya”失败：SDK 已安装，但模型权重下载、缓存、设备或模型配置有问题。

不要使用 `--policy demo` 来掩盖真实 Laya 安装失败。Demo 只用于执行链调试。
