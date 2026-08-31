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
python -m pip install aihi-agent==0.1.0
~~~

参见 [PyPI 项目页](https://pypi.org/project/aihi-agent/0.1.0/)。它会自动安装兼容的
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
from pathlib import Path

from aihi.agent import (
    HostBackend,
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
        sandbox=HostBackend(Path.cwd(), unsafe=True),
        tools=[InspectTool()],
    )
    .with_max_turns(20)
    .build()
)

session = Session.create(
    InMemoryEventStore(),
    cwd=Path.cwd(),
    provider="fake",
    model="fake-model",
)

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

\`RuntimeBuilder\` 要求调用方提前提供关键依赖：

- \`provider\` 和 \`model\`；
- 一个 \`sandbox\` backend；
- 由应用批准的 \`tools\` 集合。

可通过以下方法显式增加可选扩展：

- \`.with_max_turns(...)\` 和 \`.with_context_window(...)\`；
- \`.with_policy(...)\`、\`.with_approvals(...)\` 和 \`.with_hooks(...)\`；
- \`.with_skills(...)\`、\`.with_memory(...)\` 和 \`.with_compaction(...)\`；
- \`.with_subagents(...)\`、\`.with_artifacts(...)\` 和 \`.with_telemetry(...)\`。

Coordinator 的默认 turn budget 是有限的（\`100\`），应用可以进一步降低它以形成产品级安全边界。

## 核心模块

| 区域 | 主要 API |
| --- | --- |
| Runtime 与 Run | \`Runtime\`、\`RuntimeBuilder\`、\`RunCoordinator\`、\`RunResult\`、\`RunState\` |
| Session 与存储 | \`Session\`、\`EventStore\`、\`InMemoryEventStore\`、\`SQLiteEventStore\`、\`Event\` |
| Context | \`ContextCompiler\`、`CompactionPolicy`、`ContextState`、摘要和 Compaction Generator |
| Tool | \`Tool\`、\`ToolSpec\`、\`ToolContext\`、`PreparedToolCall`、\`ToolRegistry\` |
| Policy 与 Approval | \`PermissionMode\`、\`DefaultPolicyEngine\`、\`Approval\`、Approval Resolver |
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
断点之后。Cache 是否可用都不会改变 Event Replay、Policy、Approval、Sandbox 或 Tool Result
持久化语义。

`ContextPressureController` 使用 `ContextBudget.input_capacity` 衡量完整的规范化请求。默认使用
保守的本地估算；达到 65% 且 Provider 声明能力时请求精确计数；计数不可用时只做降级，不使 Run
失败。`CompactionPolicy` 使用 60% 目标、70% Soft Trigger 和 85% Hard Trigger；只有预测保留的
下一轮输出会耗尽后续请求容量时，才允许在 85% 以下产生 Hard 决策。每个持久化
`model.usage` Event 都记录计数方法、当前/预测压力、Trigger、Reason 和 Target，并记录 Provider 上报的
Cache Read/Write Token 与 Cache Family Key 的 SHA-256，绝不记录完整 Key 或 Prompt。

压力达到 70% 后，Runtime 在调用 Provider 前最多尝试一次批量 Soft Pruning。它只移除旧的、成功的、
只读 Tool Result 正文，并要求原始 Message 已持久化，且 Session Scope Artifact 通过访问权限、Manifest
和 Payload 完整性校验。Tool Call、Result 标识与错误状态、Artifact 引用、近期完整 Group Tail 以及
稳定 Cache Prefix 均保持不变。未达到最小回收量时整批放弃；不可变 Event 历史永不改写。

压力达到 85%，或保留输出预测后续请求将超限时，Runtime 会用 Schema v2 `ContextState` 替换较旧的
完整 Group，并保留按 Token 选择的近期原文 Tail（20%、最大 32K、至少四个完整 Group）。文件状态、
验证收据、失败、Pending Approval、Subagent 和 Artifact 会先从不可变 Event、Tool Result Metadata 与
Artifact Manifest 确定性投影，再执行可选的模型语义补充。模型只能补充约束、决策、开放问题和下一步，
不能宣称文件已修改或验证已成功。每个 `compaction.created` v2 Event 都记录证据引用、Policy/计数元数据
和保留 Tail；v1 Event 继续可 Replay。Hard Compaction 必须达到 60% Target，否则返回
`context_window_exceeded`。

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
