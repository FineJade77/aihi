# ADR-0008：Subagent Task Graph 与父子治理边界

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0001、ADR-0002

## 决策

M6a 先建立不依赖执行后端的子代理协调边界：

1. 每个子代理是父 Run 下的 `TaskSpec`/`TaskNode`，由显式 `TaskGraph` 保存状态和父子链接；
   Graph Snapshot 是恢复输入，不能用未持久化的线程变量替代。
2. 创建子任务时强制校验 capability 集合、Token/成本/超时/Tool Call 预算和 WorkspaceScope
   都是父任务的子集；只读父工作区不能升级为可写，canonical path 不能逃逸父范围；深度和子任务
   数量有上限。
3. 状态转移只允许白名单路径。取消递归标记所有活动后代，Interrupted 后只能显式 Resume；
   完成、失败和取消是终态。每个状态变化可通过统一 Event Sink 追加 `subagent.*` 事件。
4. 子代理通信只经过有界结构化 FIFO Mailbox。发送者/接收者必须是同一 Graph 节点，消息有大小
   上限、重复 ID 拒绝和 in-flight/ack 协议；恢复时可以显式 requeue 未确认消息。
5. 本 ADR 不引入 Docker、真实 Worker、Git Worktree 或 API。未来执行后端必须消费这些 canonical
   边界，并将其权限视为上限，不能在 Worker 内重新授予能力。

## 原因

先固定可回放的权限和状态语义，可以让后续 Docker、Worker 和服务化共享同一事件与恢复协议，避免
把并发实现细节误当作安全边界。Host 仍遵循项目基线：它是首选但必须显式 `unsafe=true`，而本
ADR 的 WorkspaceScope 只表达授权范围，不声称提供隔离。
