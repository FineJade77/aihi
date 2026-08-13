# aihi-agent

[English](README.md) | **简体中文**

AIHI 的 Provider-neutral、可恢复 Agent Runtime。它把模型契约转换成带持久化、策略和安全边界的
执行系统，供具体应用组合。

## 职责

- 通过显式 `RuntimeBuilder` 运行有界的 model/tool turns。
- 追加事件日志并在中断后恢复 Session。
- 编译上下文、预算保护和派生摘要压缩。
- 通过 Policy、Approval、Hooks 和 Sandbox 执行工具。
- 提供 Skill、MCP、Plugin、Subagent、Memory、Artifact、Telemetry、Replay 和 Eval 接入点。

本包不选择 Provider、不实现 UI、不提供 Router/Gateway，也不隐藏工具默认值。

## 架构

```text
aihi.models Provider
        │
RuntimeBuilder → RunCoordinator → EventStore
        │              ├─ ContextCompiler / Compaction
        │              ├─ ToolRegistry → Policy → Approval
        │              └─ Hooks → SandboxBackend → Tool
        └─ Skills / MCP / Subagents / Memory / Artifacts
```

事件存储是事实源。工具调用先落盘再执行，并且每个调用恰好有一个结果；Policy 返回 `ASK` 时
Run 会挂起，等待应用层解决后恢复。

## 安装与最小运行

```bash
uv sync
uv pip install -e packages/aihi/agent
```

```python
from aihi.agent import RuntimeBuilder

runtime = (
    RuntimeBuilder()
    .with_provider(provider, model="my-model")
    .with_sandbox(sandbox)
    .with_tools(tool_registry)
    .build()
)
```

Provider、Sandbox 和 tools 必须由应用显式注入；不存在无条件选择这些依赖的
`default_runtime()`。

## 核心约束

- 默认 loop 有最大 turns，防止无界消耗 token。
- 读文件、Glob、Grep 等声明为并发安全的只读工具可并行执行；修改工具保持顺序。
- Host backend 必须显式 `unsafe=true`。
- Resume 使用首次 `run.started` 固化的 Provider、Model、Workspace、权限和预算。
- 子 Agent 使用独立 Session，并只能获得父级权限、预算和 workspace 的更严格子集。

## 开发

```bash
pytest packages/aihi/agent/tests
ruff check packages/aihi/agent
mypy packages/aihi/agent/src
```

参见 [架构文档](../../../docs/ARCHITECTURE.zh-CN.md) 和 [Coding Agent 文档](../code-agent/README.zh-CN.md)。
