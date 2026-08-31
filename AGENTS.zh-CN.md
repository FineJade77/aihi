# AIHI 贡献与开发指南

[English](AGENTS.md) | **简体中文**

AIHI 是用于构建可恢复、Provider-neutral Agent Runtime 和应用的 Python/TypeScript monorepo。本指南是贡献者和 Coding Agent 的项目级约束：说明代码归属、不可回归的不变式、验证命令和 Review 要求。

> 开始改代码前，请阅读 [架构文档](docs/ARCHITECTURE.zh-CN.md) 与 [任务路线图](docs/TASK.zh-CN.md)。
> `docs/adr/` 与 `docs/rfcs/` 仅作本地决策记录，已被 Git 忽略。

## 目录

- [项目结构](#项目结构)
- [选择正确的层](#选择正确的层)
- [开发环境](#开发环境)
- [验证命令](#验证命令)
- [变更流程](#变更流程)
- [Runtime 不变式](#runtime-不变式)
- [安全与沙箱](#安全与沙箱)
- [包级规则](#包级规则)
- [兼容性与持久化](#兼容性与持久化)
- [Review 清单](#review-清单)

## 项目结构

```text
packages/aihi/models/        aihi-models：模型契约与 Provider Adapter
packages/aihi/agent/         aihi-agent：可恢复 Agent Runtime
packages/aihi/code-agent/    aihi-code-agent：Coding Runtime 与 Worker
packages/aihi/code-protocol/ @aihi/code-protocol：RPC DTO 与 JSON Schema
apps/aihi-code-cli/          @aihi/code-cli：TypeScript/Ink TUI
tests/                       跨包契约、集成、打包和 fixture
docs/                        稳定架构与路线图；ADR/RFC 仅本地保存
```

依赖方向必须单向：

```text
aihi-models -> aihi-agent -> application runtime -> UI
                           aihi-code-agent     @aihi/code-cli
```

`aihi.models` 不得 import `aihi.agent`；基础包不得 import 应用；应用不得复制 Runtime 或安全实现。
`aihi.agent.agents` 是 Subagent 协调基础设施，不是用户 Agent 产品目录。

三个 Python distribution 已发布 `0.1.0`：

- [aihi-models PyPI](https://pypi.org/project/aihi-models/0.1.0/)
- [aihi-agent PyPI](https://pypi.org/project/aihi-agent/0.1.0/)
- [aihi-code-agent PyPI](https://pypi.org/project/aihi-code-agent/0.1.0/)

## 选择正确的层

| 改动 | 放置位置 | 不要放置在 |
| --- | --- | --- |
| Message、Model Request/Response、Stream Chunk、Provider Error、Adapter | `packages/aihi/models` | `aihi-agent` 或应用 |
| Runtime、Session、EventStore、Context、ToolSpec、Policy、Sandbox、Skill、MCP、Memory、Subagent、Eval、Observability | `packages/aihi/agent` | Coding Prompt、UI、产品默认值 |
| Coding Prompt、Workspace 规则、Provider/Model catalog、permission mode、Coding Tool、Worker | `packages/aihi/code-agent` | 两个基础包 |
| Worker 与客户端共享的 RPC method、DTO、Event guard、JSON Schema | `packages/aihi/code-protocol` | Runtime 状态、持久化 |
| Slash 命令、Picker、Transcript、Composer、终端展示 | `apps/aihi-code-cli` | Worker 事实、EventStore 写入 |
| 产品专属行为 | 对应应用 | 可复用基础包 |

可能跨产品复用的缺口，先在 [TASK.md](docs/TASK.zh-CN.md) 记录 `H-*` 任务。产品 Prompt、Role、Tool
bundle、凭据和 UX 留在应用层。跨包只使用顶层公共 API，禁止导入私有模块绕过边界。

## 开发环境

要求 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 20+、pnpm 9。

```bash
uv sync
pnpm install
```

正常使用时：

```bash
python -m pip install aihi-code-agent==0.1.0
```

仓库开发、需要验证源码改动时：

```bash
uv pip install -e packages/aihi/models
uv pip install -e packages/aihi/agent
uv pip install -e packages/aihi/code-agent
```

不要为了方便单个测试而随意增加依赖；应同步更新 package manifest、打包测试和 README。

## 验证命令

先跑最相关的检查，再在交付前跑完整门禁：

```bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
pnpm --dir apps/aihi-code-cli typecheck
pnpm --dir apps/aihi-code-cli test
pnpm --dir packages/aihi/code-protocol typecheck
```

聚焦测试：

```bash
python3 -m pytest packages/aihi/models/tests
python3 -m pytest packages/aihi/agent/tests
python3 -m pytest packages/aihi/code-agent/tests
python3 -m pytest packages/aihi/agent/tests/security
```

打包验证：

```bash
mkdir -p dist/pypi
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/models
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/agent
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/code-agent
python3 -m twine check dist/pypi/*
```

打包测试必须覆盖 wheel 布局、PEP 420 namespace 共存、`py.typed`、依赖 metadata、installed-wheel smoke
和冻结 fixture。不能为了让新实现通过而重新生成冻结 fixture。

## 变更流程

1. **确定范围**：找到负责包、公共 API 和对应 `H-*/P-*` 任务。
2. **阅读契约**：检查架构章节、包 README 和既有测试。
3. **先写测试**：补 unit、contract、security、integration、packaging 或 UI 回归测试。
4. **通过注入点实现**：产品选择留在应用，副作用保持统一治理工具链。
5. **运行聚焦检查**：metadata 或目录布局变化时运行 installed-wheel 检查。
6. **同步文档**：英文/中文 README、架构和任务文档保持一致。
7. **Review diff**：移除生成文件、凭据、本地日志和无关格式化。
8. **一个可审查提交**：不要混入无关清理，并说明验证命令。

公共 API 仅使用 `aihi.models.__all__` 与 `aihi.agent.__all__`。公共符号变化要更新 contract test 和 README；
Event Schema、Protocol 或安全默认值变化要更新架构/任务文档，并在本地 ADR/RFC 记录决策理由。

## Runtime 不变式

- 执行 Tool 前先持久化 Assistant Tool Call。
- 每个已执行 Tool Call 恰好有一个 durable Tool Result；等待 Approval 时可暂时未配对。
- Policy 返回 `ASK` 时追加 `approval.requested` 并挂起 Run 到 `WAITING_APPROVAL`，不得伪造结果继续。
- Event Log 是事实源；ephemeral Model Chunk 不能替代 durable Message/Result。
- Resume 复用首次 `run.started` 的 Provider、Model、Workspace、Sandbox、permission mode、Prompt 摘要和 output budget。
- `INTERRUPTED` 可恢复；`CANCELLED` 是主动放弃，不可恢复。
- Session 只有一个 Writer，`seq` 单调递增，追加使用 `expected_seq` 检测冲突。
- Provider Fallback 不能盲目重放可能产生副作用的 Tool。
- 首个 Provider Stream Chunk 后不得自动 Retry 或切换 Provider。
- Event、Error、Message、Tool Result 必须可 JSON 序列化并可重新加载。
- 子 Agent 使用独立 Session，只能获得父权限、预算和 Workspace 的更严格子集。

## 安全与沙箱

所有副作用都经过：

```text
Tool input -> validation -> policy/approval -> hooks -> sandbox -> durable Tool Result
```

- `ToolSpec` 管理执行治理；`ModelToolDefinition` 只含模型可见字段。
- Policy 评估前先校验和规范化 Tool input。
- 只读且并发安全的 Tool 可并行；修改或未声明安全的 Tool 串行，结果按调用顺序提交。
- `process.exec` Tool 必须显式 Approval；`accept_edits` 不授权进程执行。
- 命令敏感路径检查是启发式，不是安全边界。
- Hook、MCP、Plugin Tool 不得绕过 Policy、Approval、Sandbox。
- `HostBackend` 必须显式 `unsafe=true`，且不提供系统隔离；隔离后端能力不足时必须 fail closed。
- `require_isolation=true` 必须拒绝 Host。

## 包级规则

### aihi-models

负责模型 canonical 类型、Message codec、Provider Protocol、Adapter 和模型错误。Agent Event、Policy、
Sandbox 和执行 metadata 不得进入本包。DeepSeek 复用 OpenAI-compatible 实现并使用明确 endpoint。凭据
和 Model catalog 由应用持有；Adapter 不得静默读取环境变量。

### aihi-agent

`RuntimeBuilder` 必须显式接收 Provider、Model、Sandbox、Tools；不要增加静默选择依赖的 `default_runtime()`。
可选能力通过 `RuntimeExtensions`、`ContextContributor`、`RunRecorder` 接入。默认使用 SQLite WAL，大输出
存入 Artifact Store；Context contributor fail closed，Telemetry/recording 失败只影响观测。

### aihi-code-agent

负责 TOML 配置、Coding Prompt、Provider/Model catalog、permission mode、Worker、Coding Tool、
Skill/MCP/Subagent 接线和本地 audit。用户配置是 `~/.aihi/aihi-code.toml`，项目配置是
`<workspace>/.aihi/aihi-code.toml`，不增加配置目录 CLI 覆盖。Worker 是唯一 EventStore Writer，使用
Code Protocol 0.3。ModelRouter、ModelGateway 和跨 Provider fallback 不进入基础包。

### @aihi/code-protocol 与 @aihi-code-cli

Protocol 保持语言无关、版本化、JSON serializable。`run.start`/`run.resume` 立即返回 acceptance，进度和
终态通过 notification 发送。重连先 Replay `session.events(after_seq)`。CLI 负责 projection、viewport、
composer、picker；Worker 负责 Runtime 事实和写入。

## 兼容性与持久化

- 新 durable Event 必须登记 Agent Schema 并覆盖冻结兼容性数据。
- 删除或改变 Event 字段含义必须升级 Schema Version 并提供迁移。
- Message JSON 改动必须更新版本化 codec，并覆盖 Message → EventStore → Session reload → Replay。
- 旧 JSON、SQLite、Trace fixture 是兼容性契约，不能为了适配新代码而改写。
- Session fork 复制前缀生成独立 Session，父 Session 不可变。
- Compaction、Memory、Snapshot、Trace、Eval 都是派生数据，不得覆盖原始 Event。
- `audit.jsonl` 是脱敏、尽力而为的运维日志，不是 Runtime 事实源。
- 不提交 API Key、Token、凭据、完整环境变量、未脱敏模型/Tool 输出、本地 `.aihi` 状态或构建产物。

## Review 清单

- [ ] 负责包和依赖方向正确。
- [ ] 已识别公共 API、Event/Protocol Schema 和安全影响。
- [ ] 聚焦测试及相关质量门禁通过。
- [ ] 打包/目录布局改动已运行 installed-wheel 检查。
- [ ] 中英文文档同步。
- [ ] 未包含 Secret、生成物、本地 ADR/RFC 或无关改动。
- [ ] Commit message 描述一个可审查变更。

新增可复用能力时同步更新 [TASK.md](docs/TASK.zh-CN.md)、相关包 README 和测试。稳定契约写入架构文档，
决策理由保留在被 Git 忽略的 ADR/RFC 本地文件中。遵循仓库许可证，尊重代码 Review，优先使用测试、
契约和可复现命令提供证据。
