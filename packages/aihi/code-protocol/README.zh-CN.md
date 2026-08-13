# @aihi/code-protocol

[English](README.md) | **简体中文**

AIHI Coding Worker 边界使用的版本化 TypeScript DTO 和 JSON Schema。它是 Python
`aihi-code-agent` Worker 与 TypeScript `@aihi/code-cli` 客户端之间的语言无关契约。

## 职责

- JSON-RPC 2.0 request、response、notification 和 error 类型。
- Agent、Session、Task、Run、Approval、Skill、MCP、Tool 和配置 DTO。
- 协议版本、精确 handshake 常量、运行时 guards 和 JSON Schema。

本包不启动 Worker、不执行工具、不保存运行时状态，也不包含业务策略。Worker 实现和
Content-Length framing 分别位于 `aihi-code-agent` 与 CLI RPC client。

## 兼容性

当前协议版本为 `0.2`。客户端与 Worker 必须精确匹配版本，不能静默降级。消息是 JSON-RPC 2.0
对象，通过字节流上的 `Content-Length` 头传输。

## 方法组

| 组 | 方法 |
| --- | --- |
| Session | `session.create/list/get/events/fork/usage` |
| Task | `task.create/spawn/get/list/transition` |
| Run | `run.start/resume/list/cancel` |
| Approval | `approval.list/resolve` |
| Skill | `skill.list/trust/untrust` |
| 配置 | `config.get/init/acknowledge_host` |
| 集成 | `mcp.list`, `tool.list` |

`run.start` 与 `run.resume` 立即返回 acceptance；终态通过 `run.completed`、`run.failed`、
`run.interrupted`、`run.cancelled`、`run.error` 和 `approval.requested` 通知。

## 开发

```bash
pnpm --dir packages/aihi/code-protocol build
pnpm --dir packages/aihi/code-protocol typecheck
```

参见 [架构文档](../../../docs/ARCHITECTURE.zh-CN.md) 和 [CLI 文档](../../../apps/aihi-code-cli/README.zh-CN.md)。
