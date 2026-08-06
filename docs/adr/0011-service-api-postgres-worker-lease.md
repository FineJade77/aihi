# ADR-0011：服务化控制面、PostgreSQL Store 与 Worker Lease

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0002、ADR-0004、ADR-0008

## 决策

1. 提供可选 FastAPI 控制面适配器。应用由依赖注入的 `EventStore`、`RunLeaseStore` 和可选
   `ArtifactStore` 构造，只读写会话事件、Approval、Worker lease 和受作用域 Artifact；不得在
   HTTP 路由中直接执行 Tool、Provider 或 Sandbox。FastAPI 不进入核心必需依赖；未安装时 API 工厂
   失败为稳定 `api_unavailable`。认证、授权、TLS、限流和公网暴露由部署层负责。
2. `/sessions/{session_id}/artifacts` 只列出 `policy.session_id` 匹配且没有 `run_id` 的 Session
   Artifact；run-scoped Artifact 必须通过带匹配 `run_id` 的已知 Artifact 查询访问，persistent
   Artifact 不通过 Session 路由暴露。直接 Artifact 查询也必须提交 Session scope，并拒绝 scope
   不匹配、过期或不存在的 Artifact；错误响应不包含本地路径、Manifest 内容或后端异常。
3. 新增 `PostgresEventStore`，实现同一 `EventStore` Protocol。追加在事务中锁定 Session head
   （`SELECT ... FOR UPDATE`），比较 `expected_seq`，写入事件并原子更新 head；查询结束事务。
   通过 `psycopg` 延迟加载或注入 DB-API connection factory，核心不绑定 PostgreSQL 驱动。唯一
   约束竞态映射为稳定冲突错误，非 JSON 值 fail closed。
4. Worker 使用 `RunLease`、`RunLeaseStore` 和单调 fencing token。一个 Run 同时只能有一个有效
   owner；过期租约可被接管并获得更高 token，旧 owner 的 renew/release 失败。Lease 只是调度
   所有权，不替代 Policy、Approval、Capability Lease 或 Sandbox。

## 原因

控制面需要支持进程外 Worker，但不应复制 Runtime 的副作用入口。保持 SQLite/PostgreSQL 同一
事件协议可以让本地开发与生产部署共享恢复和并发语义；fencing token 防止网络分区或停顿的旧
Worker 在新 owner 接管后继续写入。

## 后续

- 将 lease 控制状态与事件 outbox/持久化投影结合，支持 Worker 崩溃恢复和租约续期监控；
- 在部署层接入认证、组织/项目授权和 OTel trace context；
- 增加真实 PostgreSQL CI 与多 Worker 故障注入测试。
