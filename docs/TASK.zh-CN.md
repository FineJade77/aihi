# AIHI 任务路线图

[English](TASK.md) | **简体中文**

> AIHI monorepo 的交付计划。每项任务都包含状态、范围和验收证据。

| 字段 | 内容 |
| --- | --- |
| 状态 | 基础能力完成；应用和平台路线图持续推进 |
| 当前版本线 | Python 包已以 `0.1.0` 发布到 PyPI；Code Protocol `0.3` |
| 架构 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 最近完成 | H-19 Prompt Cache 与 ContextState v2 Compaction |

`docs/adr/` 与 `docs/rfcs/` 下的 ADR/RFC 是本地工作文件，已加入 `.gitignore`，稳定决策必须同步
到架构文档、项目 README、代码契约和测试中。

## 如何阅读

| 状态 | 含义 |
| --- | --- |
| Done | 已实现、已记录并通过相关测试 |
| In progress | 当前交付阶段，保持范围收敛 |
| Planned | 已接受方向，但尚未承诺实现 |
| Deferred | 合理需求，等待消费者或前置条件 |

`M-*` 表示基础里程碑，`H-*` 表示可复用 Harness 工作，`P-*` 表示应用/平台工作。新增任务必须
使用这些前缀，并在编码前写清验收条件。

## 已交付基线

仓库已成为多包 monorepo，并具备可运行的本地 Coding Agent 纵向链路。

| 区域 | 已交付能力 | 状态 |
| --- | --- | --- |
| `aihi-models` | Provider-neutral 消息、codec、能力、Token 估算及 Fake/OpenAI/Anthropic/OpenAI-compatible/DeepSeek Adapter | Done |
| `aihi-agent` | 可恢复 Loop、默认 turn budget、EventStore、Replay、Context/Compaction、Tool、Policy、Approval、Sandbox、Artifact、Skill、MCP、Plugin、Memory、Subagent、Eval | Done |
| `aihi-code-agent` | Coding 配置、用户/项目 `.aihi` 配置发现、Provider/Model catalog、Worker、Session/Run/Task API、Coding Tool 和 TUI 组合 | Done |
| `@aihi/code-protocol` | Code Protocol 0.3 DTO、method map、guard 和 Schema | Done |
| `@aihi/code-cli` | Ink TUI、Transcript Replay、滚动/输入体验、Session/Model picker、Slash 命令、Approval、Skill/MCP/Tool 管理和 Doctor | Done |
| 打包 | 独立 wheels、PEP 420 namespace、installed-wheel 兼容性、冻结 fixture Replay 和 PyPI `0.1.0` 发布 | Done |
| 运维 | 脱敏本地 `audit.jsonl`、Doctor 审计检查、Session 恢复和 Replay 诊断 | Done |

M0–M7 和 H-01–H-17 基础建设已完成，建立了多包边界、事件 Schema 兼容性、安全不变式、上下文预算、
可选能力和 Replay/Eval 面。历史编号保留用于 changelog 和 fixture 追溯，新工作使用下方路线图。

## 当前路线图

### P-01：Coding CLI 纵向链路

**状态：Done。** 本地 Worker/TUI 已支持配置 Provider、选择 Model、创建/恢复 Session、流式 Run、
持久化 Event、Approval、取消/恢复和 Doctor 诊断。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| P-01.1 | Code Protocol 0.2、非阻塞 Run acceptance、Error/Approval DTO | 版本 handshake、Runtime guard 和协议测试通过 |
| P-01.2 | 事件驱动 Transcript | Replay 与实时通知使用同一 reducer；序号缺口触发 Replay |
| P-01.3 | Transcript viewport 与 Composer | 终端感知滚动/跟随、Tool 折叠、多行输入和 Slash 补全 |
| P-01.4 | Session 与 Model UX | Session/Provider/Model picker、`/status`、`/doctor`、取消/恢复 |
| P-01.5 | Provider/Model catalog | 多 Provider profile、每个 Provider 多 Model、`/providers`、`/models`、TUI 展示与校验 |
| P-01.6 | 本地可运维性 | 脱敏 `audit.jsonl`、Doctor 审计检查、wheel 隔离验证和回归测试 |

### H-18：评估契约与 Harness 一致性

**状态：In progress。** 评估边界已冻结在[评估契约](EVALUATION.zh-CN.md)中。Harness 案例归属于
`evals/aihi_agent/`，必须使用脱敏、只做 Replay 的 Trace 评估 `aihi-agent`。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| H-18.1 | 带版本的评估目录和 JSON Schema | `evals/schemas/` 能校验 Harness 案例和报告；中英文契约一致 |
| H-18.2 | Harness 一致性语料和确定性 Runner | 合法/拒绝 Trace 覆盖生命周期、Approval、恢复、权限和脱敏；所有必选案例通过 |

### H-19：Prompt Cache 与 Context Compaction v2

**状态：Done。** Cache 契约、稳定 Provider Prefix、Token 压力控制、可恢复 Tool Result Pruning、
带证据的 ContextState v2 和联合评估/发布门禁均已实现。可复用 Harness 需求记录在已确认的本地 RFC
`docs/rfcs/0004-prompt-cache-and-context-compaction-v2.md`。Cache 请求契约和 Provider wire 映射属于
`aihi-models`；稳定前缀编译、Token 压力、可恢复
Tool Result 清理、结构化压缩、持久化和 Replay 属于 `aihi-agent`；产品 Prompt 和 Compact Model
选择仍由应用层负责。发布证据包括 Cache/Compaction Golden Trace、独立的
`aihi-code-agent-context-v1` 长 Session 对比、冻结兼容测试和 installed-wheel PEP 420 Smoke 覆盖。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| H-19.1 | 冻结 `CachePolicy`、System Block、`CompactionPolicy`、ContextState v2 和兼容契约 | 实现前新增契约测试保持失败；旧 ModelRequest、Message、Event 和 Summary 数据仍可解码 |
| H-19.2 | Stable Prefix 编译和 Provider Cache 映射 | 只有一个稳定断点；Cache Family Key 确定性；不支持的 Provider 语义等价 no-op；Cache Usage 规范化 |
| H-19.3 | 完整请求 Token 压力和 65/70/85/60 滞回 | 支持时在阈值附近精确计数；计数失败保守降级；避免频繁小压缩 |
| H-19.4 | 可恢复的旧 Tool Result 清理 | 只替换已持久化且有 Artifact 的完整 Result；满足最小回收量；Tool 配对、Event 历史和 Stable Prefix 不变 |
| H-19.5 | 带证据的 ContextState Hard Compaction | 先确定性投影 Event，再由模型补充；近期完整 Group 保留原文；多轮压缩保留所有关键事实并达到目标预算 |
| H-19.6 | 联合 Eval、兼容性、文档和打包门禁 | Cache/Compaction Golden Trace、长 Session Eval、冻结 Fixture Replay、installed-wheel 检查和中英文文档全部通过 |

### H-20：应用 Context 与命令 Sandbox 边界

**状态：Done。** 可复用 Harness 已保持应用无关：Tool 和 Policy 执行接收不透明的类型化应用
Context，应用持久化不透明的 Run 权限 Profile；Sandbox 从 Runtime 全局文件系统抽象收缩为显式注入的
命令执行能力。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| H-20.1 | `PreparedToolCall`、类型化应用 Context 和不透明 Run Profile | 先校验再做无副作用 Prepare；Policy 与 Tool 执行消费同一份规范化输入；Resume 拒绝 Profile 漂移 |
| H-20.2 | 纯命令 Sandbox 与 Runtime 解耦 | Sandbox 只暴露命令执行；Runtime、Session、Hook 和通用 Tool Context 不包含 workspace 或全局 Sandbox 假设；Session 只不透明持久化应用 metadata |
| H-20.3 | 通用委派 Scope | 基础 Subagent 类型不包含 Coding workspace 或权限模式；由注入的应用 Policy 证明子权限是父权限子集 |

### P-07：Coding Workspace 与权限归属

**状态：Done。** Coding Tool、canonical workspace、`AccessMode`、`RunMode` 和命令
Sandbox 选择均已归属 `aihi-code-agent`。`create_coding_session(...)` 将传入的 `cwd` 规范化为应用拥有的
Session metadata；TOML 只从该 workspace 发现配置，不能定义 workspace。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| P-07.1 | 应用拥有 Workspace 与 Coding Tool | 文件 Tool 使用受约束的本地 I/O；只有 Bash 持有 Sandbox；删除 `sandbox.root` 和 `workspace_read_only` |
| P-07.2 | `AccessMode` 与 `RunMode` Policy | `read_only`、`workspace_write`、`full_access` 和 `execute`/`plan` 的 ALLOW/ASK/DENY 矩阵均有测试；Plan 不能在同一 Run 内升级 |
| P-07.3 | Worker、Protocol 与 CLI | Code Protocol 0.3 暴露 Access/Run Mode 且不配置 workspace root；Resume 阻止权限漂移；CLI 展示实际生效模式 |

### P-06：Coding Agent 基准

**状态：Done。** 确定性语料、真实执行路径、重复采样指标、完整 CI 门禁和一份审核后的真实 Provider
基线均已实现。产品任务归属于 `evals/aihi_code_agent/`，必须评估隔离工作区中的实际结果。任务可以额外
导出 Harness Trace，但不能把 Coding Prompt、Tool 或产品 Policy 下沉到 `aihi-agent`。

| 切片 | 范围 | 验收 |
| --- | --- | --- |
| P-06.1 | Task、Fixture、Oracle 和报告契约 | `code-task.schema.json` 与 `eval-report.schema.json` 已版本化并有文档 |
| P-06.2 | 隔离任务 Runner 和确定性评分器 | 隐藏测试、回归、路径范围、安全和 Trace 结果汇总为一个机器可读报告 |
| P-06.3 | 基准语料和基线 | 初始任务类别与固定 Fixture Hash 可重复；真实 Provider pass@1 基线与脚本化 Runner 基线分开采集并审核 |
| P-06.4 | Offline/PR/Nightly/Release 自动化门禁 | `scripts/evals/run.py` 使用稳定退出码，支持重复的多模型 Live Profile，以及 Token/Tool/成本/延迟脱敏汇总；PR 保持确定性，Live 基线精确匹配 Provider/Model，Live 模式缺少显式真实 Provider + Docker/禁网配置时 fail closed |

### H-03–H-06：平台 Adapter

**状态：Planned。** 等真实远程消费者出现后再做，只能基于既有协议新增 Adapter，不改变 Runtime 语义。

| 编号 | 范围 | 前置条件 |
| --- | --- | --- |
| H-03 | PostgreSQL `EventStore` | 明确的多用户部署需求 |
| H-04 | HTTP control plane、Worker lease、IPC 认证 | 服务边界与威胁模型 |
| H-05 | 生产隔离 profile | 支持的部署目标和能力探测 |
| H-06 | 远程 Telemetry/Exporter | Sink、脱敏策略和保留策略 |

### P-02：Cowork 所需 Harness 缺口

**状态：Planned。** 待具体 Cowork 工作流明确后重新评估。只有 Provider-neutral、可复用且不包含产品
Prompt、Role 或 UI Policy 的能力才能进入 `aihi-agent`。

### P-03：平台部署

**状态：Deferred。** 依赖 H-03–H-06 和生产消费者；当前 Runtime 不包含 Service API、远程 Worker 或
PostgreSQL 实现。

### P-04 / P-05：Web 与 Desktop 客户端

**状态：Deferred。** 协议已与客户端形态解耦，但需先在 TUI 中验证 Event/Replay/Approval 契约。

### 已知后续事项

- 生成嵌套父子委派兼容性语料并补充递归 Graph Replay。
- 在项目示例中完善 Provider 凭据和 Model catalog 配置说明。
- 增加 Worker/TUI 长 Session 和重连 soak test。
- 明确后续 Python wheels 与 Protocol package 的发布/版本策略。

## Definition of Done

任务完成必须满足：公共契约和归属明确；Event、Error、Retry、Cancel 和安全语义有测试；既有 Session、
fixture 和 installed-wheel 消费者保持兼容或提供迁移；文档和示例符合实际代码；相关单元、集成、打包、
UI 测试通过；变更以一个可审查提交完成，不混入无关清理。

## 开发流程

1. 先在本文写明包边界和验收条件。
2. 先补契约/安全测试，再写实现。
3. 通过公共注入点实现，禁止跨包导入私有模块。
4. 同步更新相关 README 和 [ARCHITECTURE.md](ARCHITECTURE.md)。
5. 先跑聚焦测试，再跑全量门禁。
6. 检查 diff，排除生成文件、凭据、本地 fixture 和意外协议变更。

## 质量门禁

```bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
pnpm --dir packages/aihi/code-protocol typecheck
pnpm --dir apps/aihi-code-cli typecheck
pnpm --dir apps/aihi-code-cli test
```

打包改动必须独立构建 wheel、验证 PEP 420 和 `py.typed`，并回放冻结 Event/SQLite/Trace fixture；
TUI 改动必须覆盖 reducer/replay 和命令/picker。

## Backlog 规则

1. 本文件是唯一任务清单，不在包内创建第二份路线图。
2. 产品专属需求留在应用层，除非第二个产品证明其 Provider-neutral、可复用。
3. 平台能力只能消费既有 `EventStore`、`TelemetrySink` 或 `SandboxBackend` 协议；需要改变 Runtime
   语义时先评审边界。
4. 不为便利放宽安全默认值：Host 仍需显式 unsafe，`ASK` 必须挂起，副作用保持统一工具链路。
5. ADR/RFC 草稿仅本地保存；稳定决策应写入本文、代码契约和测试，而不是依赖未发布文件。
