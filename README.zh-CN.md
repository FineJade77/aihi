# AIHI

[English](README.md) | **简体中文**

AIHI 是一个面向可恢复 AI Agent 的轻量、Provider-neutral 基础设施，同时提供 Coding Agent
运行时和 TypeScript 终端界面。

## 特性

- 统一的模型契约，支持 OpenAI、Anthropic、DeepSeek、OpenAI-compatible 和 Fake Provider。
- 应用层支持多个 Provider profile，以及每个 Provider 的多个模型目录。
- 基于事件溯源的会话、可恢复 Run、上下文压缩、审批和有界 turns。
- 带 Policy、Sandbox、Skill、MCP、Subagent、Artifact 和脱敏审计日志的 Coding 工具链。
- Python Worker 与 TypeScript CLI 共用的 JSON-RPC 协议和 Schema。
- 支持 Session/Model 选择、审批、恢复、Skill/MCP 管理和诊断的 Ink TUI。

## 架构

```text
@aihi/code-cli (Ink TUI)
        │ Content-Length framed JSON-RPC 2.0
aihi-code-agent (Coding runtime + Worker)
        │
aihi-agent (Provider-neutral Agent loop)
        │
aihi-models (Model contracts + Provider adapters)
```

`@aihi/code-protocol` 是 Worker 与 CLI 之间共享的 DTO/Schema 边界，不是第二个运行时层。

依赖方向固定为：`aihi-models ← aihi-agent ← aihi-code-agent`。

## 仓库结构

```text
packages/aihi/models       模型契约与 Provider
packages/aihi/agent        基础 Agent Runtime
packages/aihi/code-agent   Coding Worker 与应用组合层
packages/aihi/code-protocol TypeScript 协议和 JSON Schema
apps/aihi-code-cli         TypeScript/Ink TUI
tests                      契约、集成、打包和 fixture 测试
docs                       架构与任务文档
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+
- pnpm 9+

### 安装

```bash
uv sync
pnpm install
```

### 运行检查

```bash
python3 -m pytest
ruff check .
mypy
pnpm --dir apps/aihi-code-cli test
```

### 启动 Coding CLI

```bash
pnpm --dir apps/aihi-code-cli build
pnpm --dir apps/aihi-code-cli start -- --workspace /path/to/project
```

配置文件位于用户目录 `~/.aihi/aihi-code.toml`，项目配置位于
`<workspace>/.aihi/aihi-code.toml`。详见 [aihi-code-agent 中文文档](packages/aihi/code-agent/README.zh-CN.md)。

## 文档

- [架构设计](docs/ARCHITECTURE.zh-CN.md)
- [任务路线图](docs/TASK.zh-CN.md)
- [各项目 README](packages/aihi/models/README.zh-CN.md)

ADR/RFC 仅保存在本地 `docs/adr/` 和 `docs/rfcs/`，不会提交到 Git。

## 设计原则

- 事件日志是运行时事实源，摘要、Memory 和 UI 都是派生视图。
- 所有副作用必须经过 `tool → policy → hooks → sandbox`。
- Host 执行必须显式声明 `unsafe=true`，不能被误认为安全隔离。
- 基础包不实现 ModelRouter、ModelGateway 或产品 UI。

## 许可证

MIT
