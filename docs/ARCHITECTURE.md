# Architecture v0.2 — implementation baseline

## 1. 主次关系

Laya 是运行时主决策引擎；LLM 是显式调用的策略草稿编译器。`AgentService.step` 不调用 LLM。失败、超时、低置信度、没有候选或者预算耗尽进入 `needs_context` 并释放控制，而不是退化为逐步 LLM 控制。

当前把 KnowledgePack、StrategyPack 和 FastPolicyContext 的静态部分收敛为一个有 schema/version/hash 的 `Profile`，避免第一版出现多个尚无实际区别的管理服务。动态 Observation、Goal 和候选在每一步编译成有字符/候选上限的上下文。Episode 由服务管理，不假设 Laya SDK 自带持久会话或记忆。

## 2. 数据与职责

- `Profile`：知识摘要、探针、动作模板、条件、完成条件、循环预算；不包含运行权限或可执行脚本。
- `Session`：运行实例、Profile 快照、Goal/facts、mode、run state、控制者、版本、Episode、有限待批准动作。
- `Observation`：当前结构化状态、采集时间、语义指纹、节点绑定。服务给它独立 ID。
- `Decision`：Laya 返回的候选 ID、概率、候选间隔、耗时和路由模型。
- `Action`：已解析候选模板及目标绑定，关联 observation/decision/session，带版本和 deadline。
- `ExecutionResult`：`executed/rejected/unknown/failed` 与独立的 verification 结果。点击成功不等于任务成功。
- `Event`：持久序号、因果/关联 ID、metadata；可选训练状态。SQLite journal 是执行幂等边界，不是状态自动恢复系统。

依赖方向：数据模型 → Profile/Policy/Runtime/Storage → Control → Service → API/Console。Policy 不引用 Runtime，LLM 不引用 Executor。实际副作用必须由 Service 的准入/执行路径产生。

## 3. 两种控制语义

**Transactional action**：填一个字段、点一次按钮、选择一个选项。执行前重新观察；完整语义指纹、目标节点 UID、Goal/Control/Policy/Episode 版本必须匹配。使用当前已观察的 ElementHandle，禁止 locator 在节点替换后自动指向另一个元素。

**ControlFrame**：当前短时间内期望保持的按键和有限相对指针位移。支持序号、20–250ms TTL 和 observation age。不能要求每一帧画面指纹相等，否则动态环境会让动作永远失效；仍检查上下文版本、控制权、有效期和当前动作前提。不是预先排队的长动作序列。

`MotorLease` 的 timer 与执行器使用同一个短时锁，旧 timer 不能释放新 frame 的按键。帧过期、暂停、接管、策略更新或退出时发送 neutral。该保证是应用层 soft realtime；调度暂停、驱动无响应、进程/系统崩溃仍需设备端独立 watchdog。

## 4. 版本与人机协作

```
Versions = goal_version + control_epoch + policy_version + episode_id
```

`state_version` 仅在语义指纹变化时增加，不因截图刷新增加；事务型动作还检查 fingerprint。运行 Laya 的 worker 不持有执行锁。Human 接管首先同步更新 epoch/owner，再等待短时执行锁来释放输入；晚返回的推理结果不能执行。

已经被底层接收的点击、网络请求或输入不能“撤销”。我们保证阻止之后的旧动作，不承诺抢占能回滚已经开始的副作用。操作系统层直接键鼠输入尚未统一监听，因此用户应从控制台接管；Attached Chrome 暂不允许 Auto。

模式：`manual / assist / auto`。状态：`idle / running / paused / awaiting_approval / needs_context / completed / stopped / failed`。模式与状态分别保存。创建默认 idle/human，只有显式 resume 才运行；接管不自动恢复。

审批绑定具体 Action ID、具体参数/版本/目标，单次消费。人在 60s 审批窗口内确认后，只有重新观察仍为相同指纹与目标时才续期执行 deadline，不改变动作含义。实时 frame 不进入人工审批队列，必须预先授予该会话实时权限并选 Auto。

## 5. 模型和慢路径

本地：预热 `Router`，同一 worker 保持模型加载。异步超时不等于 torch forward 已取消，所以继续跟踪原生推理 Future，忙时拒绝新请求，不积压过期决策。一次进程中默认仅一个模型推理 worker，多会话不代表可并行保证各自实时 SLA。

HTTP：复用连接，`/health` 检查已加载模型，`/v1/systemone` 推理，禁止自动跳转。解析返回候选集合及概率分布，检查 selected argmax、概率范围/和、阈值及 margin。这里的概率不是经本项目真实场景校准后的成功率，不构成安全授权。

LLM：只在用户请求编译时，携带已有 Profile/goal/guide 生成新 Profile 草稿。无需提供本地表单 facts。外部攻略、页面和模型输出全部不可信；schema 和执行权限是独立限制。首次不实现自动异步策略热更新，避免两个循环的版本切换竞态。

## 6. 感知和执行边界

Browser 使用显式 CSS 探针，不运行外部提供的 JavaScript。不采集整页历史；输入值以进程随机密钥做 HMAC 后进入私有指纹，值本身不进入模型。节点身份由当前文档内 WeakMap 标识；这不是防恶意页面篡改的密码学保证。URL 查询和 fragment 不进入模型，但其私有 HMAC 参与状态变化判断。

Android 使用 uiautomator 的结构化树，不假装截图就能理解 Canvas 游戏。基本 ADB 不支持 ControlFrame，接口明确拒绝，而不是反复 spawn `adb shell input` 冒充高频实时输入。后续设备端桥需要提供持久通道、同时多指、设备端 TTL/epoch 和断线释放。

## 7. 持久化、可观测性和恢复

动作分配唯一 ID；派发前 SQLite 原子登记 `unknown/DISPATCH_NOT_VERIFIED`。只有明确得到结果才更新。若进程在真实输入与结果记录之间崩溃，已有 ID 不再派发，避免盲目重试付款/提交等副作用。新进程不会恢复 Session 或自动执行旧日志。

EventBus 实时订阅队列容量 128，满时丢最旧 live event，不阻塞输入控制；数据库保留序号，前端检测 gap 后补拉。截图由用户显式保存为 artifact，数据库只存 hash/ID/大小。训练记录默认关闭，开启时写入有限 context/candidates 与 decision_id/action_id/outcome 关联；它不是自动评估正确性或自动微调系统。

## 8. 尚待领域实测的门槛

网页：真实模型在已知表单、多候选页面、拒绝动作、页面变化和异常重试上的成功率。游戏：状态提取误差、状态新鲜度、Laya p50/p95、输入提交/设备生效时间、端到端观察至动作时间、抖动、TTL 释放和人工接管时间必须分别测量。不能从纯模型推理 benchmark 推导 FPS 可玩性。
