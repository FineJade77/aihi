# ADR-0033：Coding Agent Worker 与 TypeScript CLI 桥接

- 状态：Accepted
- 日期：2026-08-11

## 决策

`aihi-code-agent` 以 Python Worker 运行，`apps/aihi-code-cli` 以 TypeScript
TUI 运行。两者通过版本化 JSON-RPC 2.0 通讯，第一阶段使用子进程 stdin/stdout
和 `Content-Length` framing；stdout 只允许协议帧，诊断日志写入 stderr。

协议只传输 JSON DTO，不暴露 Python Runtime 对象。共享协议 Schema 放在
`packages/aihi/code-protocol`，TypeScript 类型由该包导出；Python Worker 在
边界执行同等 Schema 校验。

Worker 在 `initialize` 结果中发布第一批应用命令：
`session.create/list/get/events`、`task.create/spawn/get/list/transition` 和
`run.start/run.resume`、`approval.list/approval.resolve`、
`skill.list/skill.trust`、`config.get`。
变更命令统一写入 Worker 所有的 Event Store，已提交事件通过 notification 推送，
CLI 断线后使用 `session.events(after_seq)` 补读。

Python Worker 是配置、模型、工具、Policy、Approval、Sandbox、Session 和
Event Store 的权威执行端。TypeScript CLI 只负责命令解析、TUI 展示和用户交互。
项目配置使用 `aihi-code.toml`：Provider 凭据仅允许通过环境变量名引用；Skill
根目录和 MCP stdio server 在 Worker 内解析，并通过 `RuntimeBuilder`、
`ToolRegistry` 接入既有 Policy/Hook/Sandbox 链路。
Skill 正文只通过显式的 `load_skill` Tool 加载；CLI 的 trust 操作只写入
Worker 管理的原子 lockfile。Approval resolve 只追加解析事件，必须再由
调用方显式执行 `run.resume`。Provider profiles 由应用配置声明，`run.start`
可以选择已配置的 Provider/模型；`run.resume` 必须继续匹配持久化的运行配置。
运行中的 `run.start`/`run.resume` 在 Worker 线程执行，模型 chunk 通过
ephemeral notification 流向 TUI；`run.cancel` 先返回请求确认，最终状态仍以
`run.interrupted`/`run.cancelled` 事件为准。

Durable 事件携带 Session sequence，ephemeral stream 事件只用于 UI；CLI 断线
后通过事件序号补读，不以 TUI 内存状态作为事实源。

## 备选方案

- Unix Domain Socket：预留为常驻 Worker 和多客户端模式的后续 Transport；
- localhost HTTP/WebSocket：不作为本地 CLI 第一阶段默认方式，避免端口发现和
  本地端口安全问题；
- gRPC/Protobuf：暂不采用，当前本地单 Worker 场景不值得引入构建和代码生成成本。

## 安全约束

- CLI 不直接执行 Agent Tool；
- CLI 不直接发送 API Key；
- Approval 必须通过 Worker RPC 解析；
- 配置、模型、Skill、MCP 的变更只影响新 Run，当前 Run 使用已持久化快照；
- Worker 崩溃后只能从 Session/Event Store 恢复，不能根据 UI 最后状态猜测执行结果。
