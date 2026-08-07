# ADR-0025：一次性 Approval

状态：Accepted
日期：2026-08-07
关联：ADR-0020（Approval 挂起态）

## 背景

ADR-0020 让 Policy 的 `ASK` 真正挂起 Run，但授权粒度只有一种：批准一次 `shell`，
该工具在**整个 Run 内**都不再询问。ADR-0020 当时把这一点登记为已知限制。

这与终端 Agent 的实际期望相反：默认应当是「就这一次」，「本 Run 都行」是用户显式选择。
`AuthorizationState` 也缺少表达「授权已用掉」的事件，因此无法实现。

## 决策

### 1. 授权携带自己的生命周期

`Approval` 新增 `one_shot: bool`。`ApprovalOutcome` 新增 `GRANTED_ONCE`：

| Outcome | 含义 |
|---|---|
| `GRANTED` | 本 Run 内对该工具持续有效（原语义不变） |
| `GRANTED_ONCE` | 只授权一次调用 |
| `DENIED` / `DEFERRED` | 不变 |

`GRANTED` 的含义**没有改变**，因此现有调用方不会被静默改语义；变化的是产品默认值。

### 2. `approval.consumed` 事件

Runtime 在一次调用被 `rule_id="approval.granted"` 放行后，如果匹配的授权是一次性的，
就追加 `approval.consumed`，投影随即移除该授权。消费路径覆盖两种来源：Resolver 当场批准，
以及带外 `aicode approve --once` 后 Resume 再使用。

投影 fail closed：消费未知授权、重复消费、消费 Run 级授权、`run_id` 不匹配，都会让
`AuthorizationState` 抛错，而不是静默放行。

### 3. 终端默认改为一次性

`y` = 只这一次，`a` = 本 Run 都行，`n` = 拒绝，其他/EOF = 挂起。
`aicode approve` 新增 `--once`。

## 后果

- 与 Claude Code 一类产品的默认行为一致：默认最小授权，扩大授权是显式动作；
- `approval.consumed` 是新增事件类型，审批率指标的分母需要相应理解；
- 并行工具调用共享同一个一次性授权在理论上存在竞态，但需要审批的工具都是 mutating，
  而 mutating 工具永远单独执行（ADR-0023 之后的并行规则），因此当前不可达。
