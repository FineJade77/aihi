# ADR-0004：Artifact 作用域与生命周期

- 状态：Accepted
- 日期：2026-08-05

## 决策

Artifact 仍使用不可变 Payload + Manifest，但 Manifest 增加 `ArtifactPolicy`：

- `run`：绑定 `session_id + run_id`，只允许当前 Run 访问；
- `session`：绑定 `session_id`，允许该 Session 的后续 Run 访问；
- `persistent`：无 Session/Run 所有者，兼容显式共享产物。

作用域参与 Artifact ID 的派生，因此相同文本在不同作用域下不会共用可访问对象。读取必须
提供匹配的 `ArtifactAccess`；删除和过期清理还必须显式声明 `allow_delete=True`。Runtime
上下文 Artifact 默认使用 Session 作用域；Runtime 删除入口通过 `artifact.created`、
`artifact.deleted` 记录生命周期审计，Store 的 `delete` 只负责受控物理删除。
`expires_at` 是硬过期边界，过期对象不可读取且不出现在普通列表中，清理器负责回收其 Payload
和 Manifest。

## 原因

仅以内容 Hash 去重会让不同 Session 的同一工具输出共享 Manifest，容易把 Artifact 引用误当成
权限。将作用域纳入对象身份保持了内容完整性校验，同时提供最小权限的读取和删除边界。事件
仍是审计事实源，Store 的删除不会覆盖历史事件。

## 约束

- Payload 永不就地修改；Manifest 损坏或 Hash 不匹配时拒绝读取；
- Store 层不执行模型或工具逻辑，只提供受控存储操作；
- Host 仍是 `unsafe=true` 的本地执行后端，Artifact 作用域不等同于沙箱隔离。
