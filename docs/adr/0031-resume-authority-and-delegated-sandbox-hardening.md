# ADR-0031：Resume 权限固化与子代理 Sandbox 收窄

状态：Accepted
日期：2026-08-11
关联：ADR-0020、ADR-0023、ADR-0030，ARCHITECTURE §4.1、§8、§11，TASK H-16

## 背景

双包迁移后的审查发现四个 P0 缺口：

1. `RunCoordinator.resume()` 由调用方再次提供权限、模型与 Prompt 等参数，调用方可以在恢复时
   弱化原 Run 的权限或改变执行语义；
2. 操作者在 Run 挂起期间从带外拒绝 Approval 后，Resume 会再次返回 `ASK`，而不是为原 Tool
   Call 提交唯一的拒绝结果；
3. `TaskGraph` 虽然校验了子任务 Workspace 是父 Workspace 的子集，但子 Run 仍可能拿到父
   Sandbox 实例，类型层的授权收窄没有变成执行层约束；
4. `OpenAICompatibleProvider` 可以继承 OpenAI 的默认 endpoint，使兼容渠道的 Key 被误发往
   OpenAI endpoint。

这些问题都位于恢复或委派的信任边界，不能留给应用层约定解决。

## 决策

### 1. 首次 `run.started` 是 Resume 配置的事实源

首次执行在 `run.started` 中持久化并锁定：`model`、Provider 名、Sandbox descriptor、规范化
Workspace Root、`unsafe`、`permission_mode`、Capability Lease 开关、System Prompt SHA-256 和
`max_output_tokens`。不持久化 Prompt 明文。

Resume 默认从首次事件恢复配置；调用方显式传入的值只能与首次配置一致。任何不一致都在追加
`run.resumed`、用户消息或执行工具之前拒绝。非空 System Prompt 由于只持久化摘要，恢复时必须
重新提供原值并通过摘要校验；空 Prompt 可安全恢复。

新增字段是 Event Schema v1 的向后兼容附加字段，不改变旧字段含义，因此不提升
`EVENT_SCHEMA_VERSION`。旧事件缺少字段时仍可读取；缺少 Prompt 摘要的旧 Run 在 Resume 时要求
调用方显式提供 Prompt，其他缺少值采用旧版本当时的默认值。

### 2. 带外拒绝绑定到原 Tool Call

Resume 按 `run_id + tool_call_id` 投影最近一次 `approval.requested` 及其 resolution。若它已经被
拒绝，则不创建新 Approval，直接为该 Tool Call 提交一个 `permission_denied` Tool Result，让
Run 按正常模型循环继续。拒绝只消费一次；Tool Call 仍满足“最终唯一 Tool Result”不变式。

### 3. 子 Run 获得收窄后的 Sandbox View

`ChildRunSubagentRunner` 必须接收父 Sandbox，并根据已通过 `TaskGraph` 校验的
`WorkspaceScope` 创建内部 `ScopedSandboxBackend`。Coordinator Factory 的契约改为
`(TaskSpec, SandboxBackend) -> ChildCoordinator`，子 Coordinator 只能拿到这个收窄后的 backend。

Scoped Sandbox 同时执行：

- canonical root containment 与 symlink escape 校验；
- `allowed_paths` 二次约束；
- `read_only` 写入拒绝；
- 列表结果过滤到委派根；
- descriptor 的 `mount_scope` 记录实际委派根。

现有通用 `SandboxBackend.run_command()` 没有表达命令级 cwd/mount scope 的能力。只读委派或根、
allowed paths 被收窄时，Scoped Sandbox 对进程执行 fail closed；仅完整、可写且与父 Sandbox
同根的委派可转发命令。这不把 Host 宣称为安全隔离边界，Host 仍须显式 `unsafe=true`。

### 4. OpenAI-compatible endpoint 必须显式提供

`OpenAICompatibleProvider` 的 `base_url` 改为必填 keyword-only 参数，并拒绝空白值。该参数表示
完整 Chat Completions endpoint；基础包不猜测兼容厂商 URL，也不读取环境变量。DeepSeek 继续由
`DeepSeekProvider` 固定到官方兼容 endpoint。

## 后果

- Resume 不能切换模型、Provider、Sandbox、Workspace、权限模式、Lease 规则、Prompt 或输出预算；
- 带外拒绝不再造成无限重复审批；
- 子任务 Workspace 的类型约束落实为文件执行约束；不能可靠收窄的进程执行被拒绝；
- `ChildRunSubagentRunner` 的构造器新增必填 `sandbox`，Coordinator Factory 从一参变为二参；
- `OpenAICompatibleProvider` 不再有默认 endpoint，调用方必须迁移为
  `OpenAICompatibleProvider(key, base_url=full_chat_completions_endpoint)`；
- 旧 JSON/SQLite 兼容语料保持原样，并由新实现继续加载和回放；新增字段另由 writer-side 契约测试
  冻结。

## 未采纳

- **只在应用层记住原参数**：事件日志才是系统事实源，进程内配置不足以支持崩溃恢复；
- **把 System Prompt 明文写入 `run.started`**：会扩大敏感内容的持久化面，摘要足以验证一致性；
- **只依赖 `TaskGraph` 的 Workspace 子集校验**：它只能证明声明没有越权，不能约束实际 I/O；
- **为通用兼容 Provider 保留 OpenAI 默认 endpoint**：便利性不足以抵消凭据误投递风险。
