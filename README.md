# aiharness

一个 **AI Coding Agent 的运行时**——模型之外的全部：手、眼、记忆和安全边界。

对标 Claude Code 一类的产品形态：核心难点在上下文管理、工具执行安全、会话可恢复，而不是编排抽象的可插拔性。

## 状态

基线已确认，第一阶段实现进行中。

本地默认执行后端为 Host。由于 Host 不能提供真正的系统隔离，启用时必须显式传入
`unsafe=true`，并把该事实写入运行事件。Docker 是可选后端。

`aiharness` 是库，不带 CLI；命令行和产品默认值属于 `aicode/` 这样的应用层。

## 文档

- [Agent 开发规范](AGENTS.md) —— 项目级 Coding Agent 约束与安全不变式
- [架构设计](docs/ARCHITECTURE.md) —— 分层、核心契约、Provider、权限、中断与上下文管理
- [运行时 RFC](docs/rfcs/0001-runtime-architecture.md) —— 控制面、执行面、事件模型和演进路径
- [Host 沙箱 ADR](docs/adr/0001-host-sandbox-default.md) —— Host 优先但强制显式 unsafe 的决策
- [任务分解](docs/TASK.md) —— M0–M7 分阶段任务与验收标准、风险登记

## 一句话架构

```
L0 内核（≤2000 行，写错了天花板锁死）
  对话表示 · 会话日志 · 事件流 · Provider 协议 · Tool 协议
L1 能力层（加法，删掉重来成本低）
  工具集 · skills · MCP · hooks · subagent
L2 产品层（最后做）
  TUI · commands · 主题
```

内核只表达**意图**，厂商机制留在适配器。任何以厂商术语命名的内核字段都是设计错误。
