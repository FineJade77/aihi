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
10. [ADR-0030](docs/adr/0030-aihi-multi-package-boundary.md)
11. [ADR-0031](docs/adr/0031-resume-authority-and-delegated-sandbox-hardening.md)
12. [ADR-0032](docs/adr/0032-tool-spec-ownership.md)

## 项目目标

AIHarness 是可复用的 Agent 基础设施，不是某一个具体 Agent 产品。目标发布为两个基础包：
`aihi-models` 提供模型契约与 Provider，`aihi-agent` 依赖前者并提供完整 Agent Runtime。它们负责
会话、上下文、模型适配、工具执行、策略、安全、记忆、Skill、Subagent、评估和可观测性；模型
不是系统事实源，事件日志才是。Coding、Cowork（多人/多角色协作）或其他形态的 Agent 在应用层
组合这些能力。本仓库当前不建设应用层，`aihi-code-agent` 必须等两个基础包完成后再单独确认。

## 目录边界

```text
packages/aihi/models/
  pyproject.toml
  src/aihi/models/    # Model contracts, codecs, Provider Protocol/adapters
  tests/
packages/aihi/agent/
  pyproject.toml
  src/aihi/agent/
    _core/            # Private Agent events, IDs, errors, schema/migrations
    runtime/          # Agent state machine and run coordinator
    sessions/ context/ tools/ policy/ hooks/ sandbox/
    plugins/ mcp/ memory/ skills/ agents/ artifacts/
    observability/ evals/
  tests/
tests/
  integration/        # Installed-wheel integration
  packaging/          # PEP 420, wheel and py.typed checks
  fixtures/           # Frozen compatibility corpus
```

依赖方向必须单向：`aihi.models ← aihi.agent ← application`，应用也可以直接组合
`aihi.models`。`aihi.models` 不得 import `aihi.agent`；两个基础包不得反向 import 任意应用。
`aihi.agent` 内部通过 Protocol 使用 Provider、Store、Tool、Policy、Hook 和 Sandbox。
应用之间也不得互相 import。应用负责 Prompt、模型/Provider 组合、Agent 角色、工具集合、配置和
交互体验；基础包负责可复用实现和公共契约。`aihi.agent.agents` 是 Subagent TaskGraph/协调
基础设施，不代表面向用户的 Agent 产品。

跨包和应用层只能使用 `aihi.models.__all__`、`aihi.agent.__all__`（叶子顶层 `__all__` 是唯一受
支持的组合面，内部子模块路径不承诺兼容），不复制基础实现，
也不得把产品专属 Prompt、项目规则、凭据、终端 UI 或产品默认 Policy 写回核心包。若应用
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
- Resume 必须沿用首次 `run.started` 固化的模型、Provider、Sandbox、工作区、权限、Prompt 摘要和
  输出预算；调用方不得在恢复时弱化或漂移配置（ADR-0031）。
- Provider 产生首个 Stream Chunk 后不得自动 retry 或切换；未来应用 Gateway 只能作为普通
  `Provider` decorator，不能控制 Run 恢复或 Tool 重放。
- 子代理的权限、预算和工作区只能是父 Run 的子集；派生必须经过工具链路，子 Run 在独立
  Session 中执行，权限模式取父子中更严格者；WorkspaceScope 必须落实为收窄后的 Sandbox，
  不能可靠收窄的进程执行 fail closed（ADR-0023、ADR-0031）。
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
3. 持久化 `run.interrupted` 并置为 `INTERRUPTED`（可恢复）；显式放弃走 `abandon()`，
   写 `run.cancelled` 并置为 `CANCELLED`（不可恢复，ADR-0024）；
4. 保证下一次 Resume 不会留下孤儿 Tool Call。

## Provider、Tool 和 Plugin 规则

- `aihi.models` 只拥有模型 canonical 类型，厂商字段只能存在于 Adapter 内或 opaque payload 中；
  Event、Policy、Sandbox 和 Agent Tool 执行元数据不得进入模型包。
- `aihi.models.ModelToolDefinition` 只包含名称、描述和输入 Schema；`aihi.agent.tools.ToolSpec` 另外声明
  是否修改外部状态、并发安全、能力需求、超时和幂等策略，并向模型显式投影定义。
- Tool 输入先校验和规范化，再进行 Policy 决策。
- 命令内容的敏感路径检查是启发式，不是安全边界；命令类工具的边界是逐次审批加沙箱（ADR-0028）。
- 只读且并发安全的工具调用可以并行；有副作用的工具必须单独执行，Tool Result 始终按调用顺序提交。
- Plugin 必须通过 Manifest 和版本化 Plugin Host；不得直接 import 第三方代码进主进程。
- 项目级 Plugin 默认不信任；Skill 只向上下文注入索引，正文按需加载，不得无条件塞入系统上下文。
- 可选能力通过 `RuntimeExtensions` 注入，不再往 `RunCoordinator` 构造函数加参数；
  `ContextContributor` 失败 fail closed，`RunRecorder` 失败 fail open（ADR-0022）。
- Hook 不能绕过 Policy、Approval 或 Sandbox；Hook 自身也受同样治理。

## 存储、上下文和记忆

- 使用 SQLite WAL；任何其他后端遵循同一个 `EventStore` Protocol。
- `expected_seq` 是并发写入的必要条件，不能静默覆盖别的 Run 的事件。
- 大型工具输出写入 Artifact Store，上下文只保留预览和引用。
- 压缩至少保留目标、约束、决策、文件变化、验证结果、未解决事项和下一步。
- 长期 Memory 必须带作用域、来源、置信度和可删除能力；禁止持久化 Secret。

## 开发流程

按 [TASK.md](docs/TASK.md) 的 AIHI 多包迁移阶段推进。每个任务先补契约和测试，再写实现；不要
为了提前扩展而创建未接入 Runtime 的空抽象。

开发具体 Agent 产品时，先复用两个基础包完成应用组合；只有跨 Agent 可复用的缺口才修改
`packages/aihi/models` 或 `packages/aihi/agent`。应用代码和基础包改动必须分别补对应目录的测试；
公共契约或
安全默认值变化时同步更新 ARCHITECTURE、TASK 和必要的 RFC/ADR。ARCHITECTURE 只写稳定契约，
里程碑进度写进 TASK，单次取舍写进 ADR；不要把「当前 Mx 提供…」写进架构文档。

完成改动前至少运行：

```bash
python3 -m compileall -q packages
python3 -m pytest
```

若环境已安装开发依赖，再运行 `ruff check .` 和 `mypy`（`mypy --strict` 当前为零错误，
新增代码必须保持零错误）。新增 Provider、Store、Sandbox、
Tool 或 Plugin Host 必须补对应的 contract test；涉及安全行为必须补
`packages/aihi/agent/tests/security/`。

## 变更与安全

- 不覆盖或回滚用户已有修改。
- 破坏事件 Schema、公共 Protocol 或安全默认值时，必须新增或更新 RFC/ADR。
- 新增 durable 事件类型必须同时登记进 Agent schema 的 `DURABLE_EVENT_TYPES` 并补进
  `tests/fixtures/session_schema_v1.json` 冻结语料，否则兼容性测试失败。
- 改变既有事件字段含义必须升 `EVENT_SCHEMA_VERSION` 并注册对应迁移。
- 修改 `aihi.models` 的 Message JSON 必须同步更新版本化 codec，并通过 Message → Event Store →
  Session reload → Replay 的跨 distribution 冻结语料；旧 fixture 不得重新生成来适配实现。
- 不提交 API Key、Token、凭据、完整环境变量或未经脱敏的模型/工具输出。
- 删除文件前确认其不再被 README、代码或文档引用；本项目只使用正式的
  `docs/TASK.md` 任务文档。
