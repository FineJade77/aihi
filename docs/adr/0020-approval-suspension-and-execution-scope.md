# ADR-0020：Approval 挂起状态与 Execution 授权轴

状态：Accepted
日期：2026-08-06
关联：ADR-0001（Host 沙箱默认）、RFC-0001、TASK.md H-02

## 背景

此前 Policy 返回 `ASK` 时，`ToolDispatcher` 自行 `Approval.issue()`、发出 `approval.requested`，
并返回一个 `permission_approval_required` 的错误 Tool Result；`RunCoordinator` 把该错误直接喂回
模型继续循环。结果是：

1. Run 永远不会停下来等人；`RunState` 里也没有对应状态。人类没有任何介入点，
   `Session.request_approval/resolve_approval` 和控制面 `/approvals` 路由形同虚设。
2. 应用层只剩两种姿势：默认模式下所有变更类工具都失败，或 `ACCEPT_EDITS` 全部放行。
3. `ACCEPT_EDITS` 的判定只看 `ToolSpec.mutates`，因此它同时放行了 `shell`/`run_tests`
   这类进程执行工具；而且放行分支复用了 `default.read_only` 的 `rule_id`，
   **一次可变更工具的执行会被审计成只读放行**，污染 `policy.decided` 事件与由它派生的指标。
4. 只有 `default.mutation_requires_approval` 这一条规则会产生 `approval.requested`；
   `capability.lease_required` 的 `ASK` 没有任何授权入口，该模式必然空转。

## 决策

### 1. `ASK` 挂起 Run，而不是伪造 Tool Result

- 新增 `RunState.WAITING_APPROVAL`：非终态、可恢复。
- 新增事件 `run.suspended`（携带 `approval_id` 与 `pending_tool_call_ids`）和 `run.resumed`。
  挂起不写任何终态事件；`run.resumed` 取代重复的 `run.started`，使恢复后的会话仍可 Replay。
- 新增 `ApprovalResolver` Protocol（`aiharness.policy`）：
  `resolve(ApprovalRequest) -> ApprovalOutcome{GRANTED, DENIED, DEFERRED}`。
  未注入 Resolver 时默认 `SuspendingApprovalResolver`，即 **默认挂起**——既不自动批准也不自动拒绝。
- Approval 的签发与解决只发生在 `RunCoordinator` 侧，通过
  `Session.request_approval/resolve_approval` 落盘；`ToolDispatcher` 只报告决定，不再铸造授权。
- 挂起时被暂停的 Tool Call **不合成结果**：`repair_orphan_tool_calls` 接受 `exclude`，
  Resume 时先执行这些挂起调用，再请求模型。崩溃恢复与主动挂起因此被明确区分。
- 同一 Tool Call 上已存在未解决的 Approval 时复用它，Resume 不会重复请求。
- Approval 被拒绝时提交一个 `permission_denied` Tool Result，Run 正常继续。
- `capability.lease_required` 的 `ASK` 被批准后签发一张 run-scoped Capability Lease
  （`issued_by=approval`），关闭原先的死路。批准后若 Policy 仍返回 `ASK`，
  返回 `permission_approval_ineffective` 而不是再次询问，避免循环。

### 2. Execution 是独立于 `mutates` 的授权轴

- `DefaultPolicyEngine` 新增 `_execution_capabilities = {"process.exec"}`。
- 任何声明该能力的工具一律 `ASK`（`default.execution_requires_approval`），
  `ACCEPT_EDITS` 不覆盖它；`PLAN` 模式同时拒绝 `mutates` 和执行类工具。
- `ACCEPT_EDITS` 放行工作区写入时使用独立 `rule_id="mode.accept_edits"`，
  `default.read_only` 从此只用于真正的只读放行。
- 显式 Approval（`approval.granted`）仍可授权执行类工具——它是人的决定，不是模式默认值。

## 后果

- `--accept-edits` 恢复其字面语义：自动接受工作区编辑，运行命令仍需逐次批准。
- `policy.decided` 事件的 `rule_id` 可以直接用于审批率/拒绝率统计，不再混淆。
- Replay 必须理解 `run.suspended` / `run.resumed`；`ReplayEngine` 已相应放宽并加校验
  （`run.suspended` 必须发生在 `waiting_approval` 状态）。
- `RunCoordinator.resume()` 现在要求显式 `run_id`；`RunCoordinator.suspended_runs(session)`
  从事件流列出可恢复的 Run。

## 已知边界（登记为后续任务）

- ~~Approval 的作用域是 run + 工具名~~ —— 已在 ADR-0025 解决：`ApprovalOutcome.GRANTED_ONCE`
  产生一次性授权，用掉后追加 `approval.consumed`；`GRANTED` 仍是本 Run 持续有效。
- `process.exec` 之外的执行类能力（例如未来的 `network.egress`）需要在
  `_execution_capabilities` 中显式登记，Policy 不做启发式推断。
- 命令内容本身仍不做语义分析；本 ADR 只保证「执行必须有人点头」，
  不声称能识别危险命令。
