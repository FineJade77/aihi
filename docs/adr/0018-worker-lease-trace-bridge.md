# ADR-0018：Worker Lease/IPC Trace Bridge

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0011、ADR-0017

## 决策

1. 使用 `WorkerLeaseEnvelope` 在 Worker IPC 边界传递 lease identity、owner、expiry、fencing token、
   attempt 和严格 W3C `traceparent`；schema、整数类型、ISO 时间和 trace IDs 均 fail closed。
2. `WorkerLeaseTraceBridge.acquire/renew/release` 委托已有 `RunLeaseStore`，不复制 Lease 所有权逻辑，
   不接受 TraceContext 作为授权或 fencing 替代。无效 parent carrier 在 acquire/renew 前拒绝；旧 owner
   的 fencing token 失效时，Trace refresh 不能使其恢复权限。
3. 新 Worker takeover 使用传入的 parent carrier 创建新的 child span；同一 Worker renew 在没有新
   carrier 时保留当前 span，进程重启后若没有 carrier 则拒绝恢复。Envelope 不包含 Token、Secret 或
   原始模型/工具内容。
4. Bridge 不直接开启网络或 IPC。HTTP、Unix socket、队列等通道由宿主注入并继续遵守认证、TLS、
   Policy 和部署层的 Worker 所有权治理。

## 原因

Lease 的 fencing 和 Trace 的 parent/child 是两个不同的安全域。把它们通过显式 envelope 关联，可以
让跨进程 Worker takeover、重试和恢复在观测后端可区分，同时保持原有 stale owner 拒绝行为不变。

## 后续

- 在 FastAPI/IPC adapter 中接入 envelope 的认证、签名或 mTLS 传输；
- 增加多 Worker、进程重启和 Postgres lease store 的端到端 trace refresh 测试。
