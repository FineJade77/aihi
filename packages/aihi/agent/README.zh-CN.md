# aihi-agent

[English](README.md) | **简体中文**

面向 AIHI 的 Provider-neutral、可恢复 Agent Runtime。

\`aihi-agent\` 将模型契约转换为可持久化的执行系统，提供 Agent loop、Session、Tool、Policy、
Approval、Sandbox 边界、Context 管理、集成能力和可观测性，供应用组合成具体产品。

## 职责

- 通过显式 Runtime 组合运行有界的 model/tool turns。
- 持久化只追加的 Event Log，并在中断后恢复 Session。
- 编译支持 Stable Prefix 的 Context，并生成不改写历史的派生状态和 Compaction。
- 通过校验、Policy、Approval 和 Hook 注册、治理和执行 Tool。
- 通过显式选择的命令 Sandbox backend 执行模型生成的命令。
- 集成 Skill、MCP Server、Subagent、Memory、Artifact、Telemetry、Replay 和 Eval。

本包**不**选择 Provider、不实现 UI、不提供 Model Router/Gateway，也不隐藏 Tool 默认值；这些选择
由应用传给 \`RuntimeBuilder\`。

## 架构

~~~text
Model Provider (aihi-models)
              │
              ▼
RuntimeBuilder ──► Runtime / RunCoordinator ──► EventStore
              │                  │
              │                  ├── ContextCompiler / Compaction
              │                  ├── ToolRegistry ──► Policy ──► Approval ──► Hooks ──► Tool
              │                  │                                               │
              │                  │                                command Tool ──┘
              │                  │                                      │
              │                  │                                      ▼
              │                  │                         SandboxBackend.run_command
              │
              └── Skills / MCP / Subagents / Memory / Artifacts / Telemetry
~~~

Event Store 是事实源。Tool Call 会在执行前记录，并且每个调用恰好有一个 Result。Policy 返回
\`ASK\` 时会挂起 Run，之后可以恢复执行。

## 安装

已发布版本：

~~~bash
python -m pip install aihi-agent==0.2.0
~~~

参见 [PyPI 项目页](https://pypi.org/project/aihi-agent/0.2.0/)。它会自动安装兼容的
\`aihi-models\` 依赖。仓库开发使用：

~~~bash
uv sync
~~~

本地 editable 安装：

~~~bash
uv pip install -e packages/aihi/agent
~~~

\`aihi-agent\` 要求 Python 3.11+，依赖 \`aihi-models\` 0.1.x。

## 最小 Runtime

~~~python
from aihi.agent import (
    InMemoryEventStore,
    RuntimeBuilder,
    Session,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)
from aihi.models import FakeProvider, FakeStep, Message


class InspectTool:
    spec = ToolSpec.define(
        name="inspect",
        description="Return application-owned inspection data.",
        input_schema={"type": "object", "properties": {}},
        concurrency_safe=True,
        mutates=False,
    )

    async def run(self, input, context: ToolContext) -> ToolExecutionResult:
        return ToolExecutionResult("Application inspection completed.")


provider = FakeProvider([FakeStep(text="I inspected the workspace.")])
runtime = (
    RuntimeBuilder(
        provider=provider,
        model="fake-model",
        tools=[InspectTool()],
    )
    .with_max_turns(20)
    .build()
)

session = Session.create(InMemoryEventStore(), metadata={"application": "example"})

result = await runtime.coordinator.run(
    session,
    model=runtime.model,
    user_message=Message.text("user", "Inspect this project."),
)
print(result.state)
~~~

Sandbox backend 只暴露命令执行，不提供文件读取、Glob 或写入 API。实际命令 Tool 若环境支持应优先
使用隔离 backend。\`HostBackend\` 是受控的本地执行 backend，不是安全隔离边界，并且要求显式确认
\`unsafe=True\`。

## Runtime 组合

\`RuntimeBuilder\` 要求调用方提前提供应用级关键依赖：

- \`provider\` 和 \`model\`；
- 由应用批准的 \`tools\` 集合。

命令 Tool 可以在自己的构造函数中单独要求 \`SandboxBackend\`；Runtime 不拥有、也不向所有 Tool
分发全局 Sandbox。

`Session.create(...)` 只接收应用拥有的 JSON metadata，以及通用的 identity 和 observer 选项。Harness
只负责不透明地持久化和复制这些 metadata，不要求也不解释 cwd、Provider、Model、Workspace 或产品权限模式。

可通过以下方法显式增加可选扩展：

- \`.with_max_turns(...)\` 和 \`.with_context_window(...)\`；
- \`.with_policy(...)\`、\`.with_approvals(...)\` 和 \`.with_hooks(...)\`；
- \`.with_skills(...)\`、\`.with_memory(...)\` 和 \`.with_compaction(...)\`；
- \`.with_subagents(...)\`、\`.with_artifacts(...)\` 和 \`.with_telemetry(...)\`。

通用 Subagent 治理只负责能力与预算子集、深度和子任务数量。Harness 不解释 Workspace 或产品权限模式。
启用 Subagent 的应用必须提供 child-context factory，为每个子 Run 派生自己的不透明应用权限上下文和
持久化 Run profile，并提供 Session factory 决定子 Session 的位置与创建方式。

Coordinator 的默认 turn budget 是有限的（\`100\`），应用可以进一步降低它以形成产品级安全边界。

## 核心模块

| 区域 | 主要 API |
| --- | --- |
| Runtime 与 Run | \`Runtime\`、\`RuntimeBuilder\`、\`RunCoordinator\`、\`RunResult\`、\`RunState\` |
| Session 与存储 | \`Session\`、\`EventStore\`、\`InMemoryEventStore\`、\`SQLiteEventStore\`、\`Event\` |
| Context | \`ContextCompiler\`、`CompactionPolicy`、`ContextState`、摘要和 Compaction Generator |
| Tool | \`Tool\`、\`ToolSpec\`、\`ToolContext\`、`PreparedToolCall`、\`ToolRegistry\` |
| Policy 与 Approval | \`PermissionContext\`、\`DefaultPolicyEngine\`、\`Approval\`、Approval Resolver |
| Sandbox | \`HostBackend\`、\`LocalIsolatedBackend\`、\`DockerBackend\` |
| 集成 | Skill、MCP、Plugin、Subagent、Memory、Artifact |
| 可观测性 | \`Telemetry\`、\`JsonlTelemetrySink\`、\`InMemoryTelemetrySink\` |
| 验证 | Replay、Golden Task、Eval 和 Contract Helper |

## Tool 与 Approval 模型

Tool 通过显式 \`ToolSpec\` metadata 注册。Policy Engine 决定某次调用是允许、拒绝还是需要
Approval。Approval Lease 可以根据应用 Policy，将决定限定到一次请求、某个 Tool 或整个 Run。

应用可以通过 `ToolContext.app_context` 和 `PermissionContext.app_context` 传递类型化的不透明状态。
Tool 可以实现确定性且无副作用的 `prepare()`，返回 `PreparedToolCall`；Policy 和实际执行随后共享同一份
规范化输入。`RunCoordinator.run_profile` 会持久化 JSON 应用权限快照，并在同一 Run Resume 时拒绝不同快照。

Harness 不提供产品级文件或 Shell Tool。应用拥有自己的 Tool 集合和本地资源语义。模型生成的命令 Tool
应在构造时显式接收命令 Sandbox；普通 Tool 不会通过 `ToolContext` 获得 Sandbox。

## 可观测性

Telemetry 是观察流，不是 Event Log。\`JsonlTelemetrySink\` 输出脱敏、有界的记录，并默认创建
仅 Owner 可读写的文件。恢复使用 Event Store；运维诊断查看 Telemetry Stream，不要把 UI 输出当作事实源。

## 稳定 Context 前缀

`ContextCompiler` 将应用提供的 Base System Prompt 编译为稳定 `TextBlock`，并把 Runtime
`ContextSection` 放在动态后缀。`RunCoordinator` 使用该稳定前缀和规范化的模型可见 Tool
Definition 派生唯一 Cache Family Key。Memory、Skill、Compaction State 和当前 Turn 保持在缓存
断点之后。Cache 是否可用都不会改变 Event Replay、Policy、Approval、命令执行或 Tool Result
持久化语义。

`ContextPressureController` 使用 `ContextBudget.input_capacity` 衡量完整的规范化请求。默认使用
保守的本地估算；接近 60% 且 Provider 声明能力时请求精确计数；计数不可用时只做降级，不使 Run
失败。输出与安全空间已由 `input_capacity` 一次性预留。`CompactionPolicy` 在 80% 水位做唯一一次
Compaction 决策，目标为 60%。每个持久化 `model.usage` Event 都记录计数方法、当前压力、决策、Reason
和 Target，并记录 Provider 上报的 Cache Read/Write Token 与 Cache Family Key 的 SHA-256，绝不记录
完整 Key 或 Prompt。

Context 组装与压缩是两个独立阶段。`ContextAssembler` 保持应用 System Prompt 为稳定缓存前缀，追加
动态 Section，并在 Token 计量前外置大型 Tool Result；未注入 Artifact Store 时也会生成带摘要指纹的
有界 Head/Tail 投影。`ContextCompactor` 以能够闭合所有 Tool Call/Result 的最小 Exchange 为原子单位，
用累计的 Schema v2 `ContextState` 替换旧分组，并仅按 30%/32K Token 预算选择近期原文后缀，同时保留最新
闭合分组。累计状态自身限制在约 2K Token；没有固定 Turn 数下限，也没有第二套 Pruning 模块。

文件状态、验证收据、失败、Pending Approval、Subagent 和 Artifact 会先从不可变 Event、Tool Result
Metadata 与 Artifact Manifest 确定性投影，再执行可选的模型语义补充。模型只能补充约束、决策、开放问题
和下一步，不能宣称文件已修改或验证已成功。首次压缩后，有界状态成为权威输入，后续只投影其
`event_cursor` 之后的 EventStore 增量，并按观测序号淘汰事实。Compact Model 分块使用有界并发且逐块
降级。每次实际替换只产生一个 `compaction.created` Event；原始 Message 与 Tool Result Event 永不重写。
如果状态与最新闭合分组仍无法放入输入容量，Run 返回 `context_window_exceeded`。

应用可以只使用持久化 Event 派生 Cache 与 Compaction 诊断：

```python
usage = [event for event in session.events if event.type == "model.usage"]
compactions = [event for event in session.events if event.type == "compaction.created"]
cached = sum(int(event.data["cached_input_tokens"]) for event in usage)
input_tokens = sum(int(event.data["input_tokens"]) for event in usage)
cache_hit_ratio = cached / input_tokens if input_tokens else 0.0
```

在源码 Checkout 中运行 `python3 -m scripts.evals.run --mode pr`，会执行 Replay Golden Trace、成对的
长 Session Cache/Compaction 门禁和 Coding Agent Smoke Benchmark。

## 开发

~~~bash
uv run pytest packages/aihi/agent/tests
uv run ruff check packages/aihi/agent
uv run mypy
uv run python -m build --wheel --no-isolation packages/aihi/agent
~~~

参见仓库的[架构文档](../../../docs/ARCHITECTURE.zh-CN.md)以及应用层组合示例
[code-agent README](../code-agent/README.zh-CN.md)。

## 安全模型

- 凭据保留在应用/Provider 边界，不得写入 Prompt 或 Event payload。
- 将模型输出、Tool 参数、Skill、MCP 响应和 Subagent 输出视为不可信输入。
- 不要声称 \`HostBackend\` 可以隔离进程；需要隔离时使用 \`LocalIsolatedBackend\` 或 \`DockerBackend\`。
- 暴露 Tool 给模型前设置有限的 turn limit，并审查 Approval/Policy 默认值。
