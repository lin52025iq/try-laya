# Profile：从攻略到受控候选动作

## 导入流程

1. 用 `GET /api/v1/profiles/schema` 取得当前 JSON Schema，或参考两个内置 JSON。
2. 在外部 LLM/人工编辑器中整理知识摘要、状态特征及候选条件。已知页面探针由实际页面确认；不要凭攻略编造 DOM selector 或游戏特征。
3. `agentctl check-profile path.json` 检查格式，再在控制台导入。
4. 创建使用该 Profile 的会话，或显式“应用到会话”。后者使旧决策失效并暂停，确认后再恢复。

也可配置 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY`，由控制台生成草稿。`POST /api/v1/strategist/compile` 的结果始终 `activated: false`。

## 核心字段

`id/name/domain` 标识域；domain 为 browser/android/realtime。`knowledge_summary` 是跨局可复用摘要；`decision_instructions` 是每次选择的短规则；`probes` 将名字绑定为 CSS selector 或 Android `resource-id=...`、`text=...`、`content-desc=...`。

`actions` 是有限动作空间：click/fill/select/key/tap/control。运行时根据 when、节点存在性/唯一性、disabled 和可用 facts 做可执行性过滤；最多 8 个有效候选，再加 `__wait__` / `__escalate__`。**过滤前提用于可执行性，而不是把所有战术判断提前写成只剩一个正确答案的规则，再宣称模型学会了决策。**

条件对象：`{"path":"fields.name.empty","op":"eq","value":true}`。操作为 eq/ne/lt/lte/gt/gte/truthy/falsy；路径不存在时任何条件均不满足，不执行 eval。动作的 `expect` 是执行后验证条件，`done_when` 是整个 Episode 的完成条件。

`value_ref` 引用用户会话 facts，不把明文值嵌进候选或模型上下文。密码字段要求人工输入。普通 ADB 的 fill 是向已聚焦字段输入，必须先单独 tap 并重新 observe；它不保证清空既有文本，也不支持通用 Unicode。

## 示例：一个可导入的已知页面表单

```json
{
  "schema_version": 1,
  "id": "minimal-form",
  "name": "Known page form",
  "domain": "browser",
  "knowledge_summary": "Fill the name once, then wait for human review.",
  "decision_instructions": "Choose fill_name when the name field is empty.",
  "probes": {"name": {"selector": "#name"}},
  "actions": [{
    "id": "fill_name", "description": "Fill the empty name field using the supplied fact",
    "kind": "fill", "target": "name", "value_ref": "name",
    "when": [{"path": "fields.name.empty", "op": "eq", "value": true}],
    "expect": [{"path": "fields.name.empty", "op": "eq", "value": false}]
  }],
  "done_when": [{"path": "fields.name.empty", "op": "eq", "value": false}],
  "max_observation_age_ms": 3000,
  "tick_ms": 100,
  "max_steps": 5,
  "provenance": "human-reviewed"
}
```

这个示例只适用于你确认包含 `#name` 的页面；schema 通过不意味着它适用于其他网站。

## 实时 Profile

参考 `builtin_profiles/realtime.json`。`probe.kind=number` 从指定元素提取数字，三种候选同时暴露给 Laya。`controls.keys` 是有限按键集合；`dx/dy` 有界，`ttl_ms` 为 20–250。TTL 是最大保持期限，不是承诺该频率能在任意硬件稳定执行。

浏览器事务需要严格相同语义指纹；实时画面允许变化，但 observation age、当前前提、policy/goal/episode/control 版本都必须有效。`max_observation_age_ms` 是整个观察到派发的预算，不只是模型 forward 时间。过短会拒绝 CPU 推理，过长会执行过时战术，应基于实测设置。

不允许 Profile 自带 `safety`、任意 shell/JS、API 凭据或工作目录权限。授权来自独立的 Session API。攻略也不能替代视觉感知绑定和执行器能力；没有这些前提，模型会请求上下文或停下，不应编造敌人位置。
