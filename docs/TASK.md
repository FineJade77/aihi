# AIHarness 实施任务

状态：M0–M7、H-01 ~ H-17 与 P-01 已完成；Coding 应用层首个可用纵向链路已交付
架构基线：[ARCHITECTURE.md](ARCHITECTURE.md)
定位：**支撑多条 Agent 产品线的 Harness**。Coding 只是其中一条，Cowork（多人/多角色协作）
等形态同样建立在它之上。

当前基础包规模：源码约 17.2k 行，测试约 8.2k 行 / 354 用例；运行时第三方依赖仅 `httpx`；
`aihi.models` / `aihi.agent` 公共 API 分别为 51 / 134 个名字；33 篇 ADR。

## 范围与方向

2026-08-11 已确认开始 Coding 应用层的分阶段实现；基础包仍保持 Provider-neutral，应用层
通过 Worker RPC 组合 Session、Task、Runtime 与 TUI。此前范围原则继续有效：

### 1. 目标不只是 Coding

Harness 不知道自己在服务哪条产品线。Coding Agent 是第一个消费者，Cowork 等形态是下一批；
凡是只对某一条产品线成立的 Prompt、角色编排、工具选择和默认策略，都留在应用层。

### 2. 平台增强重新纳入范围（尚无实现代码）

此前的判据是「凡是需要第二台机器、第二个进程或第二个团队才有意义的能力都不做」，
2026-08-07 按此移除了约 4,000 行已实现代码。多人协作形态让「第二个团队」成为目标本身，
这条判据因此作废。

| 平台能力 | 曾经的实现 | 接入点（协议已在） |
|---|---:|---|
| HTTP 控制面 / 服务化 | `api/` FastAPI，620 行 | 公共 API + `EventStore` 投影 |
| 多 Worker / Run lease / IPC 认证 | 1,108 行 | `EventStore`（`expected_seq` 已是并发写入必要条件） |
| PostgreSQL Store | `PostgresEventStore`，366 行 | `EventStore` |
| 远程 OTel 管线与 exporter | ~700 行 | `TelemetrySink` |

**重新纳入范围 ≠ 现在实现**：它们今天一行代码都没有，按需逐项立项。约束是硬的 ——
只能以适配器形式接入既有协议，不得改变 Runtime 契约或安全默认值。需要改 `RunCoordinator`
契约才能落地的「平台能力」，说明设计走错了方向。

移除时判据仍然成立、不随本次调整回归的是：`Mailbox`/`SubagentCoordinator`（编排平台形态，
`SubagentTool` 直连 `TaskGraph` 已够）、Worktree/Patch 边界（没有执行体的校验器）、
Provider Golden/`EvalGate`（CI 工具而非 Harness 能力）、`cli/`（把 Provider 和工具集写死，
违反自身分层原则）。

### 3. 前端只做 TUI

应用层首个前端形态是终端 TUI。Web 与桌面是**待办**，不是不做。`aihi-code-agent` 与
`aihi-code-cli` 已按阶段建立，已完成 Worker RPC、Session/Task、TUI 与首个真实 Coding
loop/config/Skill/MCP 配置纵向链路，以及 Worker-owned Approval 查询/解析、Skill trust
和显式 `load_skill` 加载。TUI 已支持配置选择、Session 分支/历史、Run 列表与取消请求，
并接收模型流式 chunk；P-01 交付还包括 Git 只读工具、MCP/Tool/Skill 管理、Artifact、
Compaction 与可治理 Subagent 配置。

## 交付原则

- 每个里程碑必须有可运行的纵向链路；
- 不为假想的部署形态预留实现，只保留协议；
- 所有新能力先定义事件和可测试协议，再接入实现；
- 安全边界在第一条工具链路中建立，不延后到产品层；
- 原始事件不可覆盖，压缩、Memory 和 Eval 都只能追加派生数据；
- Host 是本地首选，但没有显式 `unsafe=true` 时任何工具都不得执行。

## M0：工程基线与文档

### 任务

- 建立 `pyproject.toml`、`src/aiharness`、`tests`、`docs/rfcs`、`docs/adr`、`plugins`、`examples`。
- 固定 Python 3.11+、代码格式、静态检查、测试命令。
- 建立公共错误码、事件 Schema Version 和兼容性策略。
- 建立依赖方向检查：`core` 不得反向依赖业务包，Provider 具体实现不得泄漏到 Runtime。

### 验收

- `python -m compileall src` 成功；
- `pytest`、`ruff`、`mypy` 命令可执行；
- 事件 JSON 可以写入并读回；
- 文档目录和 ADR/RFC 状态一致。

## M1：L0 核心与事件化会话

### 任务

- `core/`：Canonical Message、ContentBlock、ModelRequest/Response、Usage、ToolSpec、Event、ID、错误。
- `sessions/`：SQLite WAL Event Store、InMemory Store、Session Aggregate、Projection、Snapshot 接口。
- 使用 `expected_seq` 实现并发冲突检测。
- 实现 `session.created`、`user.message`、`assistant.message`、`tool.result` 等基础事件。
- 实现 Session Load、List 和进程重启后的完整恢复。

### 验收

- 任意 Message/Event 可 JSON 无损往返；
- 两个并发追加只有一个成功，另一个得到稳定冲突错误；
- 进程杀死后不会丢失已提交事件；
- 从事件重放得到的消息与实时 Projection 一致；
- 任何压缩实现都不能覆盖原始事件。

## M2：Runtime、Fake Provider 与最小工具闭环

### 任务

- `runtime/`：可恢复 Agent State Machine、Run Coordinator、取消与恢复。
- `models/`：Provider Protocol、Capabilities、7 种 Stream Chunk、Fake Provider。
- `tools/`：Tool Protocol、Schema 校验、确定性 Registry、Dispatcher。
- `sandbox/`：SandboxBackend、HostBackend、路径约束、超时和进程组清理。
- `policy/`：ALLOW/DENY/ASK、默认规则、审计事件。
- `tools/builtin/`：ReadFile 等只读工具（Glob/Grep 见 ADR-0028）。

### 验收

- Fake Provider 能完成“用户问题 → 模型回复”；
- Fake Provider 能完成“模型 Tool Call → Policy → Host ReadFile → Tool Result → 模型总结”；
- Assistant Tool Call 在工具执行前已经落盘；
- 未显式 `unsafe=true` 的 HostBackend 构造和工具执行均失败；
- `run.started`、`tool.started` 包含 Host unsafe 审计字段；
- Ctrl-C 或取消不会留下永久孤儿 Tool Call；
- Unknown Tool、非法参数、路径逃逸和敏感路径均有稳定错误码。

## M3：真实模型与工具执行面

### 任务

- `models/providers/anthropic.py`：流式文本、工具、推理载荷、重试和 usage。
- `models/providers/openai.py`：流式文本、工具、reasoning effort 和 usage。
- `models/providers/openai_compatible.py`：base URL、endpoint capability 配置。
- `models/providers/deepseek.py`：复用 OpenAI-compatible 协议的 DeepSeek 适配与推理回放。
- Model Gateway、角色路由、Fallback 和请求级超时。
- 写文件、编辑和命令执行工具（`bash`，见 ADR-0028）。
- `policy/`：路径规则、命令规则、审批、allow-once/allow-always、Capability Lease。

### 验收

- 所有 Adapter 通过同一 Provider Contract Test；
- Provider 切换不改变 Core Message/Event 格式；
- 非幂等工具在 Provider 重试时不会自动重放；
- 写工具默认 ASK，Plan 模式拒绝修改；
- 外部文件发生变化后，编辑需要重新读取并拒绝盲写；
- 命令执行的超时、取消、输出上限和进程组清理均有测试。
- Approval/Capability Lease 以事件持久化；两者只能授权对应 `run_id`，过期或撤销后失效；
  Runtime 恢复 Session 后仍能重建授权投影，默认 ASK 产生可审计的 `approval.requested`。

## M4：上下文、压缩与 Artifact

### 任务

- `context/`：System Prompt、项目上下文、Skill 索引、Memory 注入、Tool Schema 编译。
- Token 估算、模型能力读取和动态预算。
- 大型工具输出外置到 `artifacts/`，上下文仅保留预览与引用。
- L0 输出截断、L1 确定性微压缩、L2 结构化语义压缩。
- 压缩源范围、摘要版本、Prompt Hash、模型和前后 Token 记录。
- Provider Context Length 错误的响应式压缩。

### 验收

- 输入预算计算不产生网络往返；
- 旧 Tool Call/Result 配对不被压缩边界切断；
- 压缩后任务目标、文件变化、验证状态和下一步可恢复；
- 原始事件和压缩前内容仍可审计；
- 200+ 轮模拟会话不会因上下文超限失败；
- Artifact 大小、访问权限和生命周期可控。

当前进度：已完成 L0/L1 的 ContextCompiler、预算保护、内容寻址 ArtifactStore、成对工具
消息压缩和 Runtime `compaction.created` 接入；已完成可注入的 L2 `SummaryGenerator` 协议、
无网络 deterministic fallback、Provider Context Length 稳定错误映射，以及每次 Run 最多一次
的响应式 L2 重试；已完成 Artifact Manifest 的 Session/Run 作用域、Retention、过期清理、
受控读取/删除和 `artifact.deleted` 审计事件。M4 已完成。

## M5：插件、Skill、Hook、MCP 与 Memory

### 任务

- `plugins/`：Manifest、发现、版本、Hash、lockfile、Plugin Host。
- `skills/`：`SKILL.md` frontmatter、分层发现、按需加载和信任策略。
- `hooks/`：生命周期总线、顺序、超时、失败策略、命令/HTTP/Prompt Hook。
- MCP Client、Server Tool Schema 和断线处理。
- `memory/`：Working/Episodic/Semantic/Procedural、候选抽取、Secret 清洗、检索和删除。

当前进度：已完成 M5a Plugin Manifest、SemVer/Harness 版本约束、无执行 Discovery、确定性
内容 Hash、默认关闭的 Trust Manager 和原子 JSON lockfile；已完成 M5b Skill frontmatter、
分层发现、作用域遮蔽、显式请求加载、内容 Hash Trust 和原子 JSON lockfile；已完成 M5c
Hook 生命周期事件、稳定顺序、超时、失败策略和治理上下文；已完成 M5d MCP JSON-RPC
Client/Server Tool Schema、内存传输、有限重连和只读重试保护；已完成 M5e 版本化隔离
Plugin Host、激活前重新 Hash/Trust 校验、能力/权限子集策略、有界进程生命周期和
`PluginRemoteTool` 统一 Dispatcher 入口；已完成 M5f Memory 的四层 canonical 类型、显式候选
提取、Secret 清洗、作用域访问、确定性检索、tombstone 删除和 `memory.candidate`/
`memory.written`/`memory.deleted` 审计事件；Memory 写入要求匹配的 `MemoryAccess`，Store
边界执行二次清洗和深拷贝。

### 验收

- 第三方 Plugin 不在主进程直接执行；
- 项目级 Plugin 默认关闭，启用有明确 Trust 记录；
- Plugin Host 激活必须显式满足当前 Run 的 capabilities/permissions 子集策略，并在协议错误、
  超时或崩溃时清理进程组；
- Hook 无法绕过 Policy 或 Sandbox；
- Hook 按优先级和注册序号稳定执行，超时与失败策略可观测；有副作用的 Hook 必须显式 Trust
  和 HookGovernance；
- Skill 正文未加载时不进入模型上下文；
- 未显式请求、未信任或内容发生变化的 Skill 必须拒绝加载；
- MCP 工具与内置工具使用同一 Policy/Hook/Sandbox 链路；
- MCP 断线时只读工具可有限重试，可能产生副作用的工具不得自动重放；
- Memory 条目可追溯、可删除、不会把 Secret 写入长期记忆。

## M6：Subagent 与执行隔离

### 任务

- `agents/`：TaskSpec、子 Session、预算、深度和取消。
- `sandbox/local`、`sandbox/docker`：OS-native 与容器执行后端。

> 范围调整（2026-08-07）：本里程碑原含 `api/` 控制面、PostgreSQL Store、Worker lease 与
> Mailbox/Worktree。它们曾实现并通过测试，现已移除；其中控制面、PostgreSQL 与 Worker
> 已于 2026-08-08 重新纳入范围（待重做），见本文件顶部「范围与方向」。

当前进度：M6a 完成子代理治理核心：`TaskSpec`/`TaskResult` canonical 类型、可快照 `TaskGraph`
状态机、父子 capability/预算/深度/只读 workspace 子集校验、取消递归收尾和 Interrupted Resume。
执行入口见 H-02（`SubagentTool`）。M6b 已加入可选 `LocalIsolatedBackend`，通过 Linux
bubblewrap 或 macOS Seatbelt 做网络、进程和 workspace 外写约束，能力不足时 fail closed；它不
宣称完整文件系统机密隔离。M6c 已加入可注入 runner 的 `DockerBackend`，默认网络关闭、容器根
只读、workspace 唯一 bind mount、资源上限和 fail-closed。

M6d（控制面 / PostgreSQL / Run lease）与 Worktree/Patch 契约曾完成，现已移除。

### 验收

- 子代理权限集合始终是父任务子集；
- 子代理崩溃后可查询状态并恢复或取消；
- 要求隔离的 Policy Profile 会拒绝 Host，即使 `unsafe=true`；
- Docker 执行事件可与 Host 执行事件统一回放；
- 单会话只有一个写者；子代理因此在独立 Session 中执行（ADR-0023）。

## M7：评估与可观测性

### 任务

- `observability/`：canonical Trace/Metric/Cost 类型、脱敏、JSONL sink。
- `evals/`：Replay、TraceGraph、Grader。
- 事件轨迹导出、离线回放和评分器接口。

> 范围调整（2026-08-07）：远程 OTel 管线、OTLP 传输、Worker trace 与 Provider Golden/EvalGate
> 已移除。`TelemetrySink` Protocol 保留，远程导出属于部署适配器；远程 OTel 已于 2026-08-08
> 重新纳入范围（待重做），Provider Golden/EvalGate 不回归。

当前进度：M7a 完成不绑定厂商的 `TraceContext`、`Observation`、`MetricPoint`、`CostRecord`、
有界 `InMemoryTelemetrySink` 和 `Telemetry` facade。Session 旁路观察已持久化事件，观测故障
fail-open 且不改变 Runtime 结果；`ephemeral` 事件不进入观测记录（ADR-0021）。Redactor 对常见
凭据、非 JSON/非有限值、超长内容和未知对象 fail-closed；成本核算拒绝负数、非有限和溢出。

M7b 完成脱敏 `TraceBundle`、严格序列号/Run/Tool 生命周期 `ReplayEngine`、JSONL `EvalDataset`、
离线 `EvalRunner` 以及 EventCount/RunState/Composite Grader。TraceBundle 在导出和加载时递归冻结
并对规范化事件计算 SHA-256；Replay 只做状态投影，绝不重新执行副作用。

M7c 完成 `JsonlTelemetrySink` 与 replay-only `GoldenTask`/`GoldenTaskGrader`。

M7d 完成跨会话审计：`TraceGraph`/`replay_graph` 组合单会话 Bundle 并校验委派链接（ADR-0027）。

M7e 完成 Runtime 接入：`RunCoordinator` 在 Run 的每个出口 flush 一次，flush 失败只是观测侧失败。

已移除：远程 OTel 管线、OTLP 传输、`WorkerTraceManager`、Provider Golden 与 `EvalGate`。
Provider 兼容性覆盖由 `packages/aihi/models/tests/contract/test_providers.py` 承担。

## AIHarness 待开发 Backlog

本节是 AIHI 基础层的持续任务清单，不是某个具体 Agent 产品的需求。
产品侧的待办（应用层重建、前端形态）见顶部「范围与方向」。

### H-01：公共组合边界与兼容性

- 状态：Done；验收：应用只依赖稳定 public API，公共 Schema 有兼容性测试。
- ✅ `aihi.models.__all__` 与 `aihi.agent.__all__` 作为两个叶子的唯一组合面；
  AST import 边界测试 + 公共 API 契约测试（导出可解析/有序、
  不拉入可选依赖）；
- ✅ Event 信封版本、迁移钩子（fail closed）、事件类型目录（durable/ephemeral/legacy）和
  冻结 v1 会话语料的兼容性测试；
- ✅ 提升规则：**先有 Runtime 注入点，再写 ADR，最后才进公共 API**。`skills`/`memory`（ADR-0022）、
  `agents`（ADR-0023）、`plugins`/`mcp`（ADR-0026）依次按此完成；目前仅 `evals` 未导出（见 H-12）。

### H-02：本地运行时完善

- 状态：Done；验收：终端可 Approval、Resume、取消并恢复完整 Tool 生命周期。
- ✅ Runtime 可选注入边界（ADR-0022）：`RuntimeExtensions` + `ContextContributor`/`RunRecorder`；
  Skill 索引进上下文、Memory 检索与候选抽取接入 Run；应用层自动组合项目 Skill 索引；
- ✅ Approval Resolver 与挂起态（ADR-0020）：`RunState.WAITING_APPROVAL`、`run.suspended`/
  `run.resumed`、`ApprovalResolver` Protocol、默认挂起、Resume 执行挂起的 Tool Call；
  应用层提供终端 Resolver 与 `run -i` / `approve` / `resume`；
- ✅ Execution 授权轴（ADR-0020）：`accept_edits` 不再放行 `process.exec` 工具，
  放行事件的 `rule_id` 与依据一致；
- ✅ Subagent 接入（ADR-0023）：`SubagentTool` 走工具链路、子 Run 独立 Session、权限模式取严、
  预算（超时/Token/Tool Call）真实生效、子代理默认不可再派生；应用层以只读授权启用；
- ✅ 终态语义（ADR-0024）：`INTERRUPTED`/`CANCELLED` 分离，`abandon()` 补上挂起 Run 的出口；
- ✅ 一次性 Approval（ADR-0025）：`GRANTED_ONCE` 与 `approval.consumed`，终端默认收紧为单次；
- ✅ 工具面重整（ADR-0028）：`bash` 取代 argv 执行，新增只读 `glob`/`grep`，搜索不再需要审批。

### H-03 ~ H-06：重新纳入范围，未立项

PostgreSQL 生产化、Worker Control Plane 与部署安全、生产隔离 profile、远程观测门禁 —— 四项
曾随「不做平台增强」的判据一并移除，2026-08-08 判据作废后重新纳入范围（见顶部「范围与方向」），
但**当前没有实现代码，也没有排期**。落地方式不变：基于保留下来的
`EventStore` / `TelemetrySink` / `SandboxBackend` Protocol 新增适配器，不改动运行时。

### H-07：Plugin/MCP 应用层接入

- 状态：Done（ADR-0026）；`register_mcp_tools`/`register_plugin_tools`、`StdioMcpTransport`、
  应用层以声明文件接入 MCP 服务器；`Tool.spec` 改为只读属性以容纳远程工具的计算属性。

### H-08：父子会话联合回放

- 状态：Done（ADR-0027）；`TraceGraph`/`replay_graph` 组合单会话 Bundle 并校验委派链接；
  修正 subagent 记录为会话级，使子会话可独立回放。
- 待办：多层嵌套委派的递归结构；从真实运行生成兼容性语料（当前 fixture 与写入端靠人工同步）。

### H-09：模型驱动的上下文压缩（compact 角色）

- 状态：Done（ADR-0029）；验收：长会话可用专用 compact 模型生成结构化摘要，且不阻塞事件循环。
- ✅ `SummaryGenerator.generate` 改为 async；只有 `compact_l2()` 需要它，`compile()` 保持同步；
- ✅ `ModelSummaryGenerator`：输入有界、回复必须落回同一 schema、任何故障降级而非失败；
- ✅ 降级留痕：`strategy` 随摘要进入 `compaction.created`，`l2_model_fallback` 可见；
- ✅ `ModelRoles.compact` + 应用层可配置压缩模型（不配置则用离线摘要器）。

### H-10：Session 分支

- 状态：Done；验收：可从任意序号派生子会话，父会话不可变，两侧都能独立回放。
- ✅ `Session.fork(at_seq=...)`：复制前缀成为普通会话，父不被写入，两侧独立回放；
- ✅ `session.forked` 从 legacy 转为 durable，冻结语料新增分支会话；
- ✅ `Session.create(metadata=...)`：分支与子代理的父链接**持久化**，不再只存在于内存
  —— 原先 `subagent_session_factory` 的链接重载后即丢失。

### H-11：Hook 的应用层入口

- 状态：Done；验收：应用层可配置「编辑后自动 format/lint」并有测试。
- ✅ 应用层用一条格式化命令注册 `FormatOnEditHook`：`mutates=True` 因而必须显式 trust
  （配置这个命令就是那次授权），只在 `governance.allows_mutation` 为真时执行，走沙箱，
  失败不影响 Run；被拒绝的编辑绝不会被格式化（有测试）；
- ✅ `HookEvent`/`HookGovernance`/`HookOutcome` 进入公共 API；
- ✅ **应用层纳入 mypy 门禁**（此前从未被类型检查，因此 `config.shell_path` 这类
  笔误可以一路进到运行时）；修正 `ChildCoordinator` Protocol —— `RunCoordinator`
  原本不满足它（第三例「自己的实现满足不了自己的 Protocol」）。

### H-12：`evals` 进公共 API

- 状态：Done；验收：`ReplayEngine`/`TraceBundle`/`TraceGraph`/`Grader` 可从顶层导入。
- ✅ 公共 API 现在区分两种面：**组合面**（注入进 Run，必须先有注入点再导出）与
  **分析面**（`evals`，只读事件日志、不组合进任何东西，因此没有注入要求）。

### H-13：从真实运行生成兼容性语料

- 状态：Done；验收：语料由真实 Run 产出，写入端变更会让兼容性测试失败。
- ✅ `tests/fixtures/corpus_builder.py` 驱动真实 Run 覆盖全部 34 个 durable 类型（4 个会话、
  112 个事件），易变字段（id/时间戳/摘要/临时路径）归一化为占位符；
- ✅ 测试对比「现场生成」与「冻结文件」，写入端变更即失败，
  用 `python tests/fixtures/generate_corpus.py` 显式重新生成并 review diff；
- ✅ 首次生成即暴露一处真实设计问题：`capability.lease.revoked` 合法地发生在 Run 终态之后，
  但 ReplayEngine 把任何带 `run_id` 的事件都当作 Run 成员。现已区分
  **推进 Run 的执行事件**与**引用 Run 的记账事件**。

### 旧单包基线：H-01 ~ H-14 全部关闭

这些任务描述的是拆包前的 `aiharness` 单包基线，保持为历史交付记录。新的基础层工作从 H-15
开始，边界由 ADR-0030 决定。

### H-14：组合边界（policy vs plumbing）

- 状态：Done；验收：应用只写产品决策，装配由 Harness 承担，且安全默认值不被隐藏。
- ✅ `RuntimeBuilder`：`provider`/`sandbox`/`tools` 必填，空工具集拒绝；
  `with_artifacts`/`with_telemetry`/`with_hooks`/`with_skills`/`with_memory`/
  `with_compaction`/`with_subagents` 各自显式开启；每个 `with_*` 返回新 builder；
- ✅ 判据写进 ARCHITECTURE：「每个合理应用是否都会做同样选择，且做错了是否无声」；
- ✅ 刻意不提供 `default_runtime()` —— 那是已删除的 `aiharness/cli` 犯过的错；
- ✅ 应用层组合代码 269 → 178 行，剩下的基本只是产品决策。

### H-15：AIHI 多 distribution 迁移

- 状态：**Done**；
- 决策：[ADR-0030](adr/0030-aihi-multi-package-boundary.md)；
- 目标：把单一 `aiharness` distribution 一次性迁移为 `aihi-models` 与 `aihi-agent`，保持事件、
  Session、Trace 和安全语义兼容；本任务不创建 `aihi-code-agent`。
- 验收记录：302 项测试通过；两个 wheel 的解包、真实安装、PEP 420 共存、叶子卸载、installed-wheel
  mypy、旧 JSON/SQLite/Trace 回放、Provider 流边界及完整安全门禁全部通过。

#### 目标依赖

```text
aihi.models
    ↑
aihi.agent

future application → aihi.agent + aihi.models
```

`aihi.models` 不得 import `aihi.agent`。`src/aihi/` 是 PEP 420 namespace 根，不包含
`__init__.py` 或根级 `py.typed`；叶子包分别维护公共 `__all__` 和 `py.typed`。

#### 模块迁移映射

| 当前模块 | 目标 | 说明 |
|---|---|---|
| `core.types` 的 Message/ContentBlock/ModelRequest/Response/Usage/Capabilities | `aihi.models` | 进入版本化模型契约 |
| 当前 `ToolSpec` | 拆分 | `aihi.models.ModelToolDefinition` 只含模型字段；`aihi.agent.tools.ToolSpec` 持有执行治理元数据 |
| 模型 `ToolCallBlock`/`ToolResultBlock` | `aihi.models` | 模型消息协议；真实执行返回改称 `aihi.agent.ToolExecutionResult` |
| `core.tokens` | `aihi.models` | Provider 和上下文预算复用同一模型 token 估算契约 |
| Provider errors | `aihi.models` | 稳定错误码和 `retryable`；不依赖 AgentError |
| 其他 `core.errors`、Event、Schema、Agent IDs、await helper | `aihi.agent._core` | 私有内部层，不新增 distribution |
| `models.base`、`transport`、Provider adapters、Fake Provider | `aihi.models` | Adapter 只实现 Provider Protocol |
| `models.gateway`、`ModelRouter`、`ModelRoles`、跨 Provider retry/fallback | **不迁入基础包** | 等未来应用层确认后实现；第一阶段无跨 Provider 路由 |
| `runtime`、`sessions`、`context`、`artifacts`、`builder` | `aihi.agent` | Builder 改为显式 `provider + model + sandbox + tools` |
| `tools`、`policy`、`hooks`、`sandbox` | `aihi.agent` | 保持统一副作用链；基础 Tool 有实现、无默认工具集 |
| `memory`、`skills`、`agents`、`plugins`、`mcp` | `aihi.agent` | 可选能力继续经 Protocol/RuntimeExtensions 接入 |
| `observability`、`evals` | `aihi.agent` | Replay 不执行 Provider/Tool/Sandbox |
| 单包公共 API | 两个叶子公共 API | 无 `aiharness` re-export shim；跨包只能导入对方顶层 `__all__` |
| 冻结 Event/Session/Trace fixtures | 根 `tests/fixtures` | 原样迁移并增加旧 SQLite，禁止重生成旧语料适配实现 |

#### 阶段与顺序

##### H-15a：兼容性门禁先行（Done）

- 固定 Message Schema v1 codec，旧 Event 缺少 `message_schema_version` 时按 v1 读取；
- 增加真实旧 SQLite Session 与旧 TraceBundle fixture；
- 先写 Message → Event Store → Session reload → Replay 跨包契约测试；
- 先写 wheel 解包、PEP 420 共存、叶子 `py.typed` 和 import 方向测试。

验收：新增门禁在尚未迁移的实现上能表达当前语义，fixture 未被重写。

##### H-15b：提取 `aihi-models`（Done）

- 创建 `packages/aihi/models/pyproject.toml` 和 `src/aihi/models`；
- 迁移模型类型、版本化 codec、Provider Protocol、transport、token estimation 和 Adapter；
- 拆出 `ModelToolDefinition`；所有 Provider 跑同一 contract suite；
- 不迁移 Router、Gateway、ModelRoles 或跨 Provider fallback。

验收：`aihi-models` wheel 可独立安装、导入和通过类型检查，import graph 不包含 `aihi.agent` 或
旧 `aiharness`。

##### H-15c：提取 `aihi-agent`（Done）

- 创建 `packages/aihi/agent/pyproject.toml` 和 `src/aihi/agent`，metadata 依赖 `aihi-models`；
- 迁移 Agent `_core`、Runtime、Session、Context、Tool/Safety 链路及可选能力；
- `ToolSpec` 持有 `ModelToolDefinition`，模型请求只接收投影；
- `RuntimeBuilder` 必填 `provider + model + sandbox + tools`；compact/subagent 显式接收自己的
  `provider + model`；
- 保持所有取消、审批、事件和副作用不变式。

验收：仅安装两个已构建 wheel 即可完成 Fake Provider 的无工具、工具、Approval、取消恢复、
Compaction 和 Subagent 集成测试。

##### H-15d：原子切换与删除旧包（Done）

- 根项目变为 workspace/tooling，不再发布 `aiharness` wheel；
- 所有测试切换到安装后的 `aihi.models`/`aihi.agent` 公共 API；
- 删除前检查 README、代码、文档、fixture generator 和配置对 `src/aiharness` 的引用；
- 删除旧实现，不保留 re-export shim，也不长期维护双写/双入口。

验收：仓库不再生成或导入 `aiharness`；卸载任一 wheel 不破坏另一个 namespace 叶子；两个 wheel
共同安装后 `aihi.models` 与 `aihi.agent` 均可导入。

##### H-15e：最终质量门禁（Done）

至少执行：

```bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
```

并验证：

- 两个 wheel 的解包路径只能是 `aihi/models/**` 与 `aihi/agent/**`，不存在顶级 `models/`、
  `agent/` 或 `aihi/__init__.py`；
- mypy 从已安装 wheel 识别两个叶子为 typed package；
- 旧 `session_schema_v1.json`、SQLite Session 和 TraceBundle 完整 replay；
- Provider 首个 Chunk 后不会 retry/switch；
- 安全、contract、integration、event compatibility 全部门禁通过。

#### H-15 完成定义

H-15a～H-15e 已全部通过。完成不自动启动应用层；必须再次确认后，才允许设计或创建
`aihi-code-agent`。

### H-16：双包 P0 安全加固

- 状态：**Done**；
- 决策：[ADR-0031](adr/0031-resume-authority-and-delegated-sandbox-hardening.md)；
- 范围：只处理审查确认的 P0，不启动应用层，也不包含 P1/P2 优化；
- ✅ `run.started` 固化 Resume 配置，恢复时禁止模型、Provider、Sandbox、Workspace、权限模式、
  Capability Lease、Prompt 摘要和输出预算漂移；
- ✅ 带外拒绝按原 `tool_call_id` 生成唯一 `permission_denied` Tool Result，不重复申请 Approval；
- ✅ 子代理 `WorkspaceScope` 落实为 scoped Sandbox，强制 root、allowed paths、read-only，不能可靠
  收窄的进程执行 fail closed；
- ✅ `OpenAICompatibleProvider.base_url` 改为必填完整 endpoint，避免兼容渠道凭据回落到 OpenAI；
- ✅ 旧 JSON/SQLite 语料不重写；新增事件字段由 writer-side 契约测试单独冻结。

验收：Resume 权限漂移、拒绝恢复、Scoped Sandbox 单元/安全测试、RuntimeBuilder 子代理端到端测试、
OpenAI-compatible 构造器契约以及完整 compile/test/lint/type 门禁全部通过。

### H-17：ToolSpec 归属整理

- 状态：**Done**；
- 决策：[ADR-0032](adr/0032-tool-spec-ownership.md)；
- `ToolSpec` 与 `IdempotencyPolicy` 实现移动到 `aihi.agent.tools.spec`；
- `aihi.agent.tools` 和 `aihi.agent` 顶层公共导出保持兼容；
- 工具契约与 Policy-aware Dispatcher 分层，包根采用延迟 Dispatcher 导入避免循环依赖；
- 运行时逻辑、事件 Schema 和安全默认值不变。

### 下一批：应用层与平台增强

按顶部「范围与方向」立项，全部处于 **Planned，无实现代码**：

| 编号 | 项 | 状态 | 说明 |
|---|---|---|---|
| P-01 | 应用层重建（TUI 前端） | Done | Worker RPC、Session/Task、Ink TUI、Provider profiles、Run 流式执行/取消/恢复、Approval/Skill/MCP/Tool 管理、Git 只读工具及 Artifact/Compaction/Subagent 配置均已接入 |
| P-02 | Cowork 形态所需的 Harness 缺口 | Planned | 先做现状评估再立项。只有 Provider-neutral、跨产品线可复用的缺口才进 Harness |
| P-03 | 平台增强（H-03 ~ H-06） | Planned | 控制面、多 Worker、PostgreSQL、远程 OTel。只能是既有协议的新增适配器 |
| P-04 | Web 前端 | 待办 | TUI 形态跑通前不开工 |
| P-05 | 桌面前端 | 待办 | 同上 |

顺序判据：**先完成 H-15；P-01 已完成；P-01 阶段内不做 P-03**。没有真实前端消费的平台能力，会重演一次「实现了、测试过、
然后按判据删掉 4,000 行」——那正是这份文档顶部记录的事。

### 待开发清单维护规则

1. 开发具体 Agent 产品时，只有当缺口是 Provider-neutral、可复用且不携带具体
   Agent Prompt/Policy 时，才提升为 `aihi-models` 或 `aihi-agent` 任务；否则留在对应 Agent 目录。
2. 任何基础包改动必须在同一变更中更新本节状态、契约/安全测试，并在破坏公共 Schema 或默认值
   时新增或更新 ADR/RFC。
3. Agent 开发过程中发现 Harness 缺口，可以先记录为 `H-*`，完成后回填实现文件、测试、ADR 和
   验收结果；不得创建第二份相互冲突的任务清单。
4. 每个任务仍遵守 [AGENTS.md](../AGENTS.md) 的流程：先补契约和测试，再写实现，最后跑全量门禁。
5. 平台能力可以做，但只能是既有协议（`EventStore`/`TelemetrySink`/`SandboxBackend`）的新增
   适配器；需要改 Runtime 契约或放松安全默认值才能落地的，先停下来写 ADR。

### 验收

- 任意 Tool Call 可定位到 Session、Run、Model Attempt、Policy、Hook 和 Sandbox；
- 能计算 Token、成本、延迟、压缩率、Tool 错误率和策略拒绝率；
- Trace Replay 能复现相同的状态转移；
- 关键安全回归失败时阻止合并。

## 风险与门禁

| 风险 | 门禁 |
|---|---|
| Host 被误认为安全沙箱 | 配置、事件和 UI 同时显示 `unsafe=true`；隔离 Profile 禁止 Host |
| 重试造成副作用重复执行 | Tool 幂等声明、Intent 事件和不可自动重放策略 |
| 取消造成孤儿 Tool Call | 取消修复协议 + 进程重启恢复测试 |
| 上下文压缩丢失任务状态 | 结构化摘要 Schema + 任务回归集 |
| 插件绕过策略 | Plugin Host 隔离 + 工具链路统一入口 |
| Provider 差异泄漏到 Agent Runtime | `aihi.models` Contract Test + 单向依赖检查 |
| 模型 Tool Schema 混入执行权限 | `ModelToolDefinition` 投影 + `aihi.agent.tools.ToolSpec` 安全测试 |
| Message 演进破坏旧 Session | Message codec 版本 + Event/SQLite/Trace 跨包冻结语料 |
| PEP 420 wheel 布局错误 | 构建后解包 + 独立/共同安装 + `py.typed` 测试 |
| 应用层耦合基础包内部实现 | 两个叶子 `__all__` + AST import 边界测试 |
| 事件 Schema 悄悄漂移 | 信封版本 fail closed + 冻结语料覆盖全部 durable 类型 |
| 平台能力侵入运行时契约 | 只能是既有协议的新增适配器；改 `RunCoordinator` 契约即先写 ADR |
| 平台能力先于消费者实现 | P-01 之前不做 P-03；没有前端消费的能力不立项 |
