# ADR-0002：事件溯源作为会话事实源

- 状态：Accepted
- 日期：2026-08-04

## 决策

Session 使用 append-only Event Store。SQLite WAL 是本地实现，PostgreSQL 是生产实现；两者
都必须提供 `append(session_id, expected_seq, events)` 的乐观并发语义。

Snapshot 只缓存 Projection，不能代替事件。Context Compaction、分支、Eval Replay 和审计
都以事件为输入。单会话由一个 Runtime Owner 写入，服务模式增加 Worker lease。

授权同样走事件溯源：`approval.requested`、`approval.resolved`、
`capability.lease.issued` 和 `capability.lease.revoked` 追加到 Session。Runtime 只使用
从这些事件投影出的、与当前 `run_id` 匹配且未过期/撤销的授权；没有匹配 pending 请求的
Approval resolution 视为事件不变式错误并拒绝恢复。

## 原因

事件日志能恢复中断前的工具意图，保留策略和审批证据，允许重放评估，并保证压缩不会破坏
原始对话。存储层差异被 `sessions/` Protocol 隔离，避免 SQLite 到 PostgreSQL 的迁移改变
Runtime 语义。
