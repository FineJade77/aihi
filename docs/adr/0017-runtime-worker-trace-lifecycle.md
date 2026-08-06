# ADR-0017：Runtime/Worker Telemetry 生命周期与 Trace Refresh

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0014、ADR-0016

## 决策

1. `RunCoordinator` 在 terminal Event 已追加后调用 `Telemetry.flush()`；Telemetry flush/close 均
   fail-open。共享 sink 不在单个 Run 结束时 close，宿主只在进程或 Worker shutdown 时显式调用
   `Telemetry.close()`。
2. `WorkerTraceManager` 为每个 Worker ID 维护 attempt 计数；首次启动和每次 refresh 都从父 Run 或
   外部 `traceparent` 创建新的 child span。恢复时不得复用旧 span ID，carrier 缺失或非法必须拒绝。
3. Worker TraceContext 只用于关联 API/Runtime/Worker/Subagent/Sandbox 的可观测性，不参与 Policy、
   Approval、Capability Lease、预算、Workspace 或 Sandbox 决策；它不是认证凭据。
4. 外部 Worker 的 HTTP/IPC 传输仍由宿主选择和治理；本 ADR 只定义 canonical TraceContext 与
   `traceparent` carrier，不自动建立网络连接或改变 Worker 所有权。

## 原因

Run terminal 时 flush 可以避免有界队列中的遥测在正常完成或异常收尾时遗失，同时保留 Telemetry
facade 的 fail-open 语义。Worker refresh 让跨进程重试、Subagent 恢复和 lease 转移在后端形成可区分
的 child span，避免把失效的旧 span 当作新执行尝试。

## 后续

- 接入真实 Worker lease/IPC 协议并补跨进程 trace refresh 端到端测试；
- 在 Worker shutdown 编排中统一调用 close，并评估持久化 spool 与优雅退出时限。
