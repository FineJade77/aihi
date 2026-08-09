# ADR-0026：Plugin 与 MCP 工具的注册路径

状态：Accepted
日期：2026-08-07
关联：ADR-0003（Plugin Host 隔离）、ADR-0006（MCP 工具边界）、ADR-0022

## 背景

`plugins/` 和 `mcp/` 的运行时契约早已完备：`PluginRemoteTool` 与 `McpRemoteTool` 都实现 Tool
协议，因此只要进入 `ToolRegistry` 就自动经过 `tools → policy → hooks → sandbox`。但两个包
**包外零引用** —— 没有任何代码把发现结果变成注册好的工具，MCP 也没有可用于真实服务器的传输。

## 决策

### 1. 注册是唯一入口

`register_mcp_tools(registry, client, server_name, allowed_tools=None)` 和
`register_plugin_tools(registry, host, allowed_tools=None)` 把远程工具装进 registry。
`McpClient.call_tool` / `PluginHost.call_tool` 仍是低层传输 API，不得直接交给 Runtime。

`allowed_tools` 按**服务端工具名**过滤：应用可以只暴露子集，而不必信任服务器自我约束。

### 2. `StdioMcpTransport`

标准 MCP 传输补齐。进程纪律沿用 Plugin Host：无 shell、独立进程组、最小环境、
消息大小上限、请求截止时间和有界关闭。传输只搬运 JSON-RPC，不解释 MCP 语义。

### 3. `Tool.spec` 改为只读属性

`Tool` Protocol 原本声明 `spec: ToolSpec`（可写变量），而两个远程工具的 `spec` 都是
计算属性 —— 于是**Harness 自己的远程工具从来不满足自己的 Tool 协议**。因为没人注册过它们，
这个矛盾一直没被发现。改为只读属性后，普通类属性和计算属性都满足。

### 4. 应用层只连声明过的服务器

应用层用一个环境变量指向 `{"servers": [...]}` 声明文件；配置畸形是错误而不是警告，
重名服务器直接拒绝。任一服务器启动失败时，已连接的客户端全部断开，registry 不留半成品。

## 后果

- `mcp` 与 `plugins` 完成「先有注入点 → 再有 ADR → 最后进公共 API」流程，成为最后两个毕业的
  能力包。仍不导出：`evals`、`api`、`cli`；
- MCP 工具的 `readOnlyHint`/`destructiveHint` 映射为 canonical `ToolSpec.mutates`，
  因此 Policy 依据的是本地 canonical 字段，而不是远端的自述；
- Plugin 激活仍要求 Trust 记录与激活前重新 Hash 校验，注册不改变这条链路。

## 修复的缺陷

子进程 teardown 时先关 `BufferedReader` 会与执行器中阻塞的 `readline()` 争夺缓冲区锁，
导致关闭挂起：一个不响应的 MCP 服务器让 0.3 秒的截止时间变成 30 秒。
改为先结束进程组、再直接关闭文件描述符（`plugins/host.py` 早已如此，本 ADR 让 MCP 对齐）。
