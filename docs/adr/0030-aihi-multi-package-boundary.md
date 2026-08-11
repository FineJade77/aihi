# ADR-0030：AIHI 多包边界与模型契约所有权

状态：Accepted
日期：2026-08-11
关联：ARCHITECTURE §2、§3、§5、§8，TASK「AIHI 多包迁移」，RFC-0001，ADR-0029

## 背景

现有旧单包把模型契约与 Provider、Agent Loop、Session、Tool、Policy、Sandbox 和扩展能力
发布在同一个 distribution 中。为了让模型适配和通用 Agent Runtime 可以独立复用，项目决定迁移为
monorepo 内的多个 distribution，并把 Coding Agent 留在后续应用层。

直接拆成互不相关的 `aihi-models` 与 `aihi-agent` 会遇到两个边界问题：Agent Loop 必须消费
`Message`、`ModelRequest` 和 `Provider`，而当前 `ToolSpec` 又同时包含模型可见 Schema 与 Agent
执行治理字段。若把整个现有 `core` 下沉到模型包，模型包会反向拥有 Policy/Sandbox 语义；若让
模型包依赖 Agent 包则形成循环依赖。

本决策不引入独立 `aihi-core` distribution。代价是 `aihi-models` 同时作为最低层的**模型契约包**
和 Provider 实现包；这个最低层地位只覆盖模型协议，不覆盖 Agent 运行时契约。

## 决策

### 1. Monorepo 发布两个 PEP 420 namespace distribution

目标目录和 import 面为：

```text
packages/aihi/models/                 distribution: aihi-models
  src/aihi/models/                    import: aihi.models

packages/aihi/agent/                  distribution: aihi-agent
  src/aihi/agent/                     import: aihi.agent
```

`src/aihi/` 是隐式 namespace，不得包含 `__init__.py` 或 namespace 根级 `py.typed`；每个叶子包
分别包含自己的 `__init__.py`、`__all__` 和 `py.typed`。两个 wheel 必须能够独立安装，也能共同
安装。根项目只承担 workspace、统一开发工具和跨 wheel 测试，不再发布旧单包 wheel。

依赖方向只有：

```text
aihi.models
    ↑
aihi.agent

future application → aihi.agent + aihi.models
```

`aihi.models` 不得 import `aihi.agent`；基础包不得 import 任意应用。跨 distribution 只能从对方
顶层公共 API 导入，应用也不得依赖两个包的内部子模块。

### 2. `aihi.models` 只拥有模型协议

`aihi.models` 提供：

- `Message` 与模型 Content Block；
- `ModelRequest`、`ModelResponse`、`Usage`、`Capabilities`；
- `ModelToolDefinition`；
- Provider-neutral Stream Chunk；
- `Provider` Protocol 与稳定 `ProviderError`；
- Message 的版本化 JSON codec；
- Fake、OpenAI、Anthropic、OpenAI-compatible、DeepSeek Provider；
- Provider 所需的 transport 与 token estimation。

它不提供 Event、Agent ID、Agent Tool Execution、Policy、Sandbox、通用顶级异常，也不提供
`ModelRouter`、`ModelGateway` 或 `ModelRoles`。Provider 构造器不得决定凭据来源、默认模型或
环境变量读取策略。通用 OpenAI-compatible Adapter 还必须显式接收完整 endpoint，不能回落到
OpenAI 默认地址（ADR-0031）。

### 3. `aihi.agent` 拥有完整的 Provider-neutral Runtime

`aihi.agent` 依赖 `aihi-models`，提供 Run 状态机、Session/Event Store、Context/Compaction、
Tool、Policy/Approval、Hook、Sandbox、Artifact、Memory、Skill、Subagent、Plugin/MCP、
Observability/Eval 和 `RuntimeBuilder`。它可以提供 Read/Write/Edit/Glob/Grep/Bash 等基础工具
实现，但不提供默认工具集合、默认 Provider、默认模型或 `default_runtime()`。

Agent 自己的 Event、ID、错误、Schema 和 migration 放在 `aihi.agent` 的私有 `_core` 层。
`_core` 是内部依赖层而非第三个 distribution；不设独立 `aihi-core` 不等于取消 Agent 包内部的
单向分层。

### 4. 模型工具定义与执行治理元数据分离

`aihi.models.ModelToolDefinition` 只包含模型可见字段：

```text
name, description, input_schema
```

`aihi.agent.tools.ToolSpec` 包含：

```text
model_definition, mutates, concurrency_safe, required_capabilities,
timeout_seconds, idempotency
```

Agent 编译 `ModelRequest` 时显式投影 `ToolSpec.model_definition`。Provider 不得接收、解释或修改
Agent 的权限与执行治理字段。模型消息中的 `ToolResultBlock` 与真实执行返回值也分开命名；后者
使用 `ToolExecutionResult`。

### 5. 模型消息序列化是跨 distribution 的版本化契约

Durable Event 会持久化 `aihi.models.Message`，因此 Message JSON 不能绕过 Agent Event migration
独立发生破坏性变化。`aihi.models` 提供带 `message_schema_version` 的版本化 codec；
`aihi.agent` 写事件时记录该版本，读取缺少版本的既有事件时按 Message Schema v1 处理。

每次模型消息 Schema 变化必须同时通过跨 distribution 冻结语料：

```text
aihi.models Message JSON
  → aihi.agent Event Store
  → Session reload
  → Replay
```

旧 `session_schema_v1.json` 不得为迎合新实现而重新生成；旧 SQLite Session 与 TraceBundle 必须由
新包完整加载和回放。Python import 路径变化本身不改变持久化 Schema，也不触发
`EVENT_SCHEMA_VERSION` 升级。若事件 payload 的含义或结构发生破坏性变化，仍必须按既有规则升级
Event Schema 并注册 migration。

### 6. Runtime 显式接收 Provider 与模型

基础 Runtime 的最小组合契约为：

```python
RuntimeBuilder(
    provider=provider,
    model=model,
    sandbox=sandbox,
    tools=tools,
)
```

模型驱动的 Compaction 和 Subagent 分别显式接收自己的 `provider + model`；基础包不使用
`ModelRoles`。未来应用可以实现 Router/Gateway，但它只能是满足同一 `Provider` Protocol 的
普通 decorator，不能控制 Run 恢复或 Tool 重放。

Provider 契约要求：一个 stream 只有一个终态；产生首个 Chunk 后不得自动 retry 或切换 Provider；
Provider 不执行工具；错误携带稳定错误码与 `retryable` 信息。第一阶段明确不提供跨 Provider
routing/fallback，而不是把现有 Gateway 隐式搬入 Agent 包。

### 7. 拆包不改变安全和恢复不变式

以下契约继续全部由 `aihi.agent` 保证：

- Assistant Tool Call 在执行前持久化；
- 每个 Tool Call 最终只有一个 Tool Result；
- `ASK` 挂起 Run；
- 所有副作用经过 `tools → policy → hooks → sandbox`；
- Host 必须显式 `unsafe=true`；
- 授权只来自持久事件投影；
- 取消与恢复不留下孤儿 Tool Call；
- Subagent 权限、预算和工作区只能收缩；
- Plugin/MCP 不能绕过 Dispatcher；
- Compaction 不覆盖原始 Event。

### 8. 应用层延后

`aihi-code-agent` 不属于本次迁移。两个基础包及其 installed-wheel、兼容性和安全门禁全部完成后，
再单独确认 Coding Agent 的 Prompt、项目规则、工具组合、Provider Gateway、Policy Profile 和 TUI。
当第二个应用需要复用 Router/Gateway 时，必须重新评估是否提取新的模型组合包，不得在应用之间
复制实现。

## 被否决的方案

### 独立 `aihi-core`

它能提供对称依赖图，但增加第三个发布单元和版本协调成本。本项目选择让 `aihi-models` 持有最小
模型契约，并用 `ModelToolDefinition` 投影阻止 Agent 安全语义下沉。

### 将完整现有 `ToolSpec` 放入 `aihi.models`

否决。`mutates`、并发、能力、超时与幂等性属于 Agent 的执行与安全治理，而不是模型协议。

### 将 Router/Gateway 留在 `aihi.models` 或移入 `aihi.agent`

否决。Provider 选择、角色、路由和 fallback 是应用组合决策；基础 Runtime 只消费显式 Provider。

### 不保留旧单包兼容 re-export 包

否决。当前没有外部 Python API 消费者，不承担无收益的双入口维护成本；但持久化 Session、Trace 和
Event Schema 的兼容性仍是硬门禁。

## 后果

- `aihi-agent` 对 `aihi-models` 是显式发布依赖；两个包不独立演进共享 Message Schema；
- 现有 `ModelGateway`、`ModelRouter`、`ModelRoles` 不进入任一基础包，ADR-0029 中关于
  `ModelRoles.compact` 所在层的决策被本 ADR 取代；异步模型摘要与降级语义继续有效；
- 当前单包公共 API 将一次性切换为两个叶子公共 API；无旧 import shim；
- 构建门禁必须检查 wheel 内容、PEP 420 共存、叶子 `py.typed` 和安装后的类型识别；
- 迁移先冻结兼容语料，再移动模型包，最后移动 Agent 包和删除旧单包源码；
- 本 ADR 只批准边界与迁移设计，不授权提前建设 `aihi-code-agent`。
