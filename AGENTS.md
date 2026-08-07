# AIHarness Agent 开发规范

本文件是参与 AIHarness 开发的 Coding Agent 的项目级约束。开始实现前先阅读：

1. [架构设计](docs/ARCHITECTURE.md)
2. [任务分解](docs/TASK.md)
3. [RFC-0001](docs/rfcs/0001-runtime-architecture.md)
4. [RFC-0002](docs/rfcs/0002-context-and-compaction.md)
5. [ADR-0001](docs/adr/0001-host-sandbox-default.md)
6. [ADR-0002](docs/adr/0002-event-store-and-snapshots.md)
7. [ADR-0003](docs/adr/0003-plugin-host-isolation.md)
8. [ADR-0004](docs/adr/0004-artifact-lifecycle-and-scope.md)
9. [ADR-0020](docs/adr/0020-approval-suspension-and-execution-scope.md)

## 项目目标

AIHarness 是可复用的 Agent Harness 基础层，不是某一个具体 Agent 产品。它负责会话、上下文、
模型适配、工具执行、策略、安全、记忆、Skill、Subagent、评估和可观测性；模型不是系统事实源，
事件日志才是。Coding Agent、个人助理或其他 Agent 应在 `aicode/`、`personal/` 等应用目录中
组合这些能力。

## 目录边界

```text
src/aiharness/
  core/            # Canonical types, events, IDs, errors；不得依赖业务包
  runtime/         # Agent state machine and run coordinator
  sessions/        # Event Store, snapshots, projection, branching
  context/         # Context compiler and compaction
  models/          # Gateway, router, provider adapters
  tools/           # Tool contract, registry, dispatcher
  plugins/         # Manifest, discovery, isolated Plugin Host
  policy/          # Rules, decisions, approvals, capability leases
  hooks/           # Lifecycle event bus
  sandbox/         # Host/Docker and future execution backends
  memory/          # Working, episodic, semantic, procedural memory
  skills/          # SKILL.md discovery and on-demand loading
  agents/          # Subagent task graph and coordination
  artifacts/       # Large outputs, patches, attachments
  observability/   # OTel, logs, metrics, cost accounting
  evals/           # Replay, datasets, graders
  api/             # Optional service API
  cli/             # CLI entry point

aicode/            # Coding Agent application layer (depends on aiharness)
personal/          # Optional personal Agent application (depends on aiharness)
```

依赖方向必须单向：`core` 不导入其他业务包；`runtime` 通过 Protocol 使用 Provider、Store、
Tool、Policy、Hook 和 Sandbox；`aicode/`、`personal/` 等应用可以直接复用 `aiharness` 已有的
Provider、Tool、Policy、Sandbox 和 Runtime 实现，但 `aiharness` 不得反向 import 任意应用目录。
应用之间也不得互相 import。应用负责 Prompt、Agent 角色、工具集合、配置和交互体验；Harness
负责可复用实现和公共契约。`aiharness/agents/` 是 Subagent TaskGraph/协调基础设施，不代表某个
面向用户的 Agent 产品。

应用层只能 `from aiharness import ...`（顶层 `__all__` 是唯一受支持的组合面，子模块路径一律
视为内部实现），不复制 Harness 实现，
也不得把 Coding-specific Prompt、项目规则、凭据、终端 UI 或产品默认 Policy 写回核心包。若应用
开发发现 Provider-neutral、可复用的 Harness 缺口，先在 [docs/TASK.md](docs/TASK.md) 的 H-* Backlog
登记，再补契约、测试和实现；仅服务于单个 Agent 的逻辑留在对应应用目录。

## 不可破坏的不变式

- Assistant Tool Call 必须在工具执行前持久化。
- 每个 Tool Call 必须有唯一 Tool Result，包括拒绝、取消和恢复结果；等待 Approval 的调用
  例外，它保持未配对直到 Resume 执行或拒绝它。
- Policy 返回 `ASK` 时必须挂起 Run（`WAITING_APPROVAL` + `run.suspended`），不得伪造
  Tool Result 让模型继续；默认（无 Resolver）行为是挂起，不是自动批准或拒绝。
- 执行进程是独立于 `mutates` 的授权轴：`accept_edits` 只覆盖工作区编辑，
  声明 `process.exec` 的工具必须有显式 Approval。放行事件的 `rule_id` 必须如实反映依据。
- 原始 Event 永不被压缩覆盖；Compaction 只生成新的 Context View。
- 所有副作用必须经过 `tools → policy → hooks → sandbox` 链路。
- Provider Fallback 不得盲目重放可能已经产生副作用的工具。
- 子代理的权限、预算和工作区只能是父 Run 的子集；派生必须经过工具链路，子 Run 在独立
  Session 中执行，权限模式取父子中更严格者（ADR-0023）。
- 事件、错误、模型消息和工具结果必须可 JSON 序列化和恢复。

## Host 沙箱基线

`HostBackend` 是本地首选，但 Host 不是安全隔离边界。

- 必须显式声明 `unsafe=true`；没有显式声明时拒绝构造和执行。
- `run.started`、`tool.started` 必须记录 `sandbox=host` 和 `unsafe=true`。
- 仍需执行 workspace canonical path、symlink escape、超时、输出上限和进程组清理。
- 不得声称 Host 提供文件或网络隔离。
- Docker 是可选后端；`require_isolation=true` 的策略必须拒绝 Host。

## Runtime 实现规则

Runtime 是显式状态机，不把状态藏在不可恢复的局部变量中。每个重要状态变化都产生事件。
流式 Token Delta 必须是 `ephemeral=True` 并经 `Session.emit` 发布，不写入 Store；
`Session.append` 拒绝 ephemeral 事件，`emit` 拒绝持久事件。无副作用的相邻事件可以用
`append_many` 合并成一个事务，但跨越工具执行边界的事件必须各自立即落盘（ADR-0021）。

取消任务时必须：

1. 收尾并取消在飞工具任务；
2. 为未完成 Tool Call 合成错误 Tool Result；
3. 持久化 `run.interrupted`；
4. 保证下一次 Resume 不会留下孤儿 Tool Call。

## Provider、Tool 和 Plugin 规则

- Core 只使用 canonical 类型，厂商字段只能存在于 Adapter 内或 opaque payload 中。
- Tool 必须声明 JSON Schema、是否修改外部状态、并发安全、能力需求、超时和幂等策略。
- Tool 输入先校验和规范化，再进行 Policy 决策。
- Plugin 必须通过 Manifest 和版本化 Plugin Host；不得直接 import 第三方代码进主进程。
- 项目级 Plugin 默认不信任；Skill 只向上下文注入索引，正文按需加载，不得无条件塞入系统上下文。
- 可选能力通过 `RuntimeExtensions` 注入，不再往 `RunCoordinator` 构造函数加参数；
  `ContextContributor` 失败 fail closed，`RunRecorder` 失败 fail open（ADR-0022）。
- Hook 不能绕过 Policy、Approval 或 Sandbox；Hook 自身也受同样治理。

## 存储、上下文和记忆

- 本地使用 SQLite WAL，生产使用 PostgreSQL；两者遵循同一个 Event Store Protocol。
- `expected_seq` 是并发写入的必要条件，不能静默覆盖别的 Run 的事件。
- 大型工具输出写入 Artifact Store，上下文只保留预览和引用。
- 压缩至少保留目标、约束、决策、文件变化、验证结果、未解决事项和下一步。
- 长期 Memory 必须带作用域、来源、置信度和可删除能力；禁止持久化 Secret。

## 开发流程

按 [TASK.md](docs/TASK.md) 的 M0–M7 和 H-* Backlog 顺序推进。每个任务先补契约和测试，再写实现；不要
为了提前扩展而创建未接入 Runtime 的空抽象。

开发 `aicode` 或其他 Agent 时，先复用已有 Harness 能力完成应用组合；只有跨 Agent 可复用的缺口
才修改 `src/aiharness`。应用代码和 Harness 改动必须分别补对应目录的测试；Harness 公共契约或
安全默认值变化时同步更新 ARCHITECTURE、TASK 和必要的 RFC/ADR。

完成改动前至少运行：

```bash
python3 -m compileall -q src
python3 -m pytest
```

若环境已安装开发依赖，再运行 `ruff check .` 和 `mypy`（`mypy --strict` 当前为零错误，
新增代码必须保持零错误；`psycopg`/`opentelemetry` 等可选依赖已在 `pyproject.toml` 中豁免）。新增 Provider、Store、Sandbox、
Tool 或 Plugin Host 必须补对应的 contract test；涉及安全行为必须补 `tests/security/`。

## 变更与安全

- 不覆盖或回滚用户已有修改。
- 破坏事件 Schema、公共 Protocol 或安全默认值时，必须新增或更新 RFC/ADR。
- 不提交 API Key、Token、凭据、完整环境变量或未经脱敏的模型/工具输出。
- 删除文件前确认其不再被 README、代码或文档引用；本项目只使用正式的
  `docs/TASK.md` 任务文档。
