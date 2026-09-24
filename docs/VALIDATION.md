# 本地验证记录 — 2026-09-24

本记录区分“真实执行器测试”“模拟策略/传输契约测试”和“没有验证”。机器可读结果见 [validation.json](validation.json)。

## 已运行

`python -m pytest -q`：**89 passed，4 skipped，0 failed**，本次约 12.6 秒。测试数量包括参数化安全/输入边界用例，不是 89 个网站或游戏。Python 3.13.5、Linux x86_64、Chromium `/usr/bin/chromium`；依赖实际版本见 [requirements-tested.txt](requirements-tested.txt)。

真实 Chromium 的内存 HTML 页面验证：填姓名 → 填公司 → 提交 → Saved → COMPLETED；三次动作各自重新观察并验证。执行策略明确是 `demo-NOT-LAYA`。另验证了 Assist 单次审批、审批后目标替换拒绝、非空字段值改变使指纹失效、截图不造成状态版本抖动、审批对相同状态重新确认期限。

实时沙盒：真实浏览器连续控制帧运行至少三步，Human 接管后停止；按键 TTL 自动释放；旧帧 timer 不会释放新帧；序号/期限检查；keydown 已到达但确认失败时仍发送 keyup。沙盒是显式数字状态与简单目标追踪，不是 FPS 视觉识别或实战。

服务测试：延迟推理在 Human/Goal/Profile/Episode 更新后丢弃；推理不持有执行锁；未知执行结果不自动重试；SQLite 执行日志幂等及崩溃窗口；事件序号与有界订阅；API/WS 鉴权、Host/Origin 校验、策略越权拒绝、无自动激活、敏感字段阻断、ADB 参数/文本/树解析边界。

模型适配测试：使用上游返回形状的 HTTP MockTransport 与 SDK FakeRouter 检查协议、概率解码、超时/原生线程仍忙时不积压请求。**这些不是实际权重推理。**

控制台视觉测试：真实 Chromium 渲染控制台，第二个真实 Chromium 执行表单；通过测试专用进程内 ASGI bridge 访问实际 FastAPI 业务，WebSocket 为测试 stub，并显式补拉数据库事件。最终三步完成、Saved、没有 JavaScript 错误。该结果不等于浏览器 HTTP/WebSocket 网络端到端测试；API/WS 协议另由 TestClient 覆盖。

安装：离线环境使用 `pip install -e . --no-deps --no-build-isolation` 成功；`agentctl doctor`、Profile 校验和显式 Demo benchmark 可运行。Demo benchmark 只用于检查输出格式，不能报告为 Laya 性能。

## 未通过或未执行的真实验收

| 项目 | 结果与原因 |
|---|---|
| 真实 Laya 固定样例推理 | 跳过；没有 SDK/权重。执行真实 warmup 退出码 2，明确错误，未使用 Demo 回退 |
| 真实 Laya → 浏览器三步闭环 | 跳过；同上，保留独立 live_laya 验收用例 |
| Android 真机/模拟器截图 | 跳过；本环境无 ADB binary 或授权 device serial |
| 浏览器网络导航/隔离 | 跳过；开发容器 Chromium 的管理策略阻止网络导航。未修改/绕过管理策略，改用内存页面验证 DOM |
| 实际游戏感知、FPS 多点控制、成绩/胜率 | 未实现和未测试；不得由沙盒结果推断 |
| 实际 LLM 生成策略、实际 Attached Chrome 连接 | 未做真实外部服务/设备验证；只有实现和相关边界测试 |

下载模型的尝试受到开发容器网络/包可用性限制；SDK wheel 下载未成功。上游源码已通过 GitHub connector 阅读并核对接口，参见 [SOURCES.md](SOURCES.md)，但接口核对不能替代真实推理。

## 如何继续做真实验收

在可下载模型的机器安装 `.[dev,laya]`，预热选定模型；运行 `RUN_LIVE_LAYA=1 AGENT_POLICY=local python -m pytest -q -m live_laya`。选择已授权 ADB 设备后运行 `TEST_ADB_SERIAL=... python -m pytest -q -m android`。这两组失败时应保留失败，不应修改为 mock 或强制选择预期答案。

模型 benchmark 分离报告加载、单步推理、候选正确率；真实游戏另测画面采集、状态提取、模型判断、输入投递和设备响应的 p50/p95/最大延迟与误动作率。未测量的数字保持未测量。
