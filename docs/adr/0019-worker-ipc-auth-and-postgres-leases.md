# ADR-0019：Worker IPC 认证边界与 PostgreSQL Lease Store

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0018

## 决策

1. Worker IPC 消息采用 canonical JSON 的 HMAC-SHA256 detached signature，签名格式包含版本和
   `key_id`；Authenticator 支持 keyring 轮换、常量时间比较，并拒绝未知字段、非法 JSON、错误
   key 或篡改内容。Secret 只存在宿主配置，不进入 envelope、响应 payload 或日志。
2. `WorkerLeaseIpcAdapter` 是 transport-neutral 的认证门面：先验证签名和 envelope，再调用
   `WorkerLeaseTraceBridge`。FastAPI Worker 路由只有调用方显式传入 adapter 才注册，默认创建的
   API 不暴露 Worker 网络端点。
3. TLS/mTLS 不在核心包内实现。HTTP、Unix socket、队列等宿主 transport 必须根据部署策略完成
   TLS/mTLS、peer identity 和密钥分发；HMAC 不能替代传输加密或主机授权。
4. `PostgresRunLeaseStore` 遵循 `RunLeaseStore` Protocol。Lease acquire/renew/release 在事务和
   `FOR UPDATE` 下执行，使用数据库 fencing sequence；current lease 使用 partial unique index，
   takeover 将旧行标记为非 current，旧 owner 的 token 永远不能恢复权限。

## 原因

签名应保护 IPC 消息完整性和来源，而不应把 trace 或 Lease 字段变成授权凭据。将认证放在显式
adapter 中可以使同一 Bridge 安全地复用于 HTTP、队列和本地 IPC，同时保持默认 API 不开放网络。
PostgreSQL 通过事务和数据库序列承载多进程 Lease 竞争，旧行保留便于审计和 stale fencing 拒绝。

## 后续

- 在部署层接入 mTLS peer identity 到 keyring/owner 映射；
- 评估签名 key rotation、撤销列表与跨区域数据库时钟策略。
