# ADR-0006：MCP 工具边界与安全重连策略

- 状态：Accepted
- 日期：2026-08-06

## 决策

MCP 通过 JSON-RPC 2.0 的 `initialize`、`tools/list` 和 `tools/call` 建立最小工具子集；
传输由 `McpTransport` Protocol 注入，内存 Transport 只用于契约测试。Server 返回的 Tool
Schema 必须映射为 canonical `ToolSpec`，其中没有明确 `readOnlyHint=true` 的工具按 mutating
处理。

`McpRemoteTool` 是唯一进入 Runtime ToolRegistry 的适配器，所有远程调用必须经过现有
`ToolDispatcher` 的输入校验、Policy、Hook 和 Sandbox 治理。MCP Client 的低层 `call_tool`
不直接暴露给模型或 Runtime。

连接失败或响应丢失时，Client 只对只读工具执行有限次数重连重试；可能产生副作用的工具不重试，
因为远端可能已经完成调用。错误边界统一为稳定的 Protocol、Transport、Remote 和 ToolNotFound
错误，不泄漏远端异常细节。

## 原因

MCP Server 是外部副作用边界，Harness 无法可靠判断请求在断线前是否已经执行。将 MCP 工具
纳入现有 ToolDispatcher，并采用保守的只读判定和不重放策略，可以复用既有安全治理并避免重复
副作用。
