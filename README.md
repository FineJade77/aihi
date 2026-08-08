# aiharness

一个 **AI Coding Agent 的运行时**——模型之外的全部：手、眼、记忆和安全边界。

对标 Claude Code 一类的产品形态：核心难点在上下文管理、工具执行安全、会话可恢复，而不是编排抽象的可插拔性。

## 状态

M0–M7 与 H-01 ~ H-13 全部完成。约 16.9k 行，17 个包，运行时依赖只有 `httpx`。

`aiharness` 是**库**，不带 CLI、不带 HTTP 服务。命令行、Prompt 和产品默认值属于 `aicode/`
这样的应用层。平台类能力（控制面、多 Worker、PostgreSQL、远程 OTel）**刻意不做**，
判据与已移除清单见 [TASK.md](docs/TASK.md#范围不做平台增强)。

本地默认执行后端为 Host。由于 Host 不能提供真正的系统隔离，启用时必须显式传入
`unsafe=true`，并把该事实写入运行事件。Docker 与 OS-native 隔离是可选后端。

## 一句话架构

```
L0 内核        对话表示 · 会话日志 · 事件流 · Provider 协议 · Tool 协议
L1 能力层      工具集 · skills · memory · MCP · plugins · hooks · subagent
L2 应用层      aicode/：Prompt · 工具选择 · CLI · 终端 TUI · 审批交互
```

内核只表达**意图**，厂商机制留在适配器。任何以厂商术语命名的内核字段都是设计错误。

## 三条支撑其余一切的不变式

1. **事件日志是事实源，模型不是。** 状态从事件投影而来；压缩只产生新的上下文视图，
   永不覆盖原始事件；`schema_version` 读不懂就拒绝，不猜。
2. **所有副作用经过 `tools → policy → hooks → sandbox`。** 派生子代理、调用插件、
   连接 MCP 服务器都是工具调用，因此都被同一条链路审批、记录、取消。
3. **拿不准就停下来问人。** Policy 返回 `ASK` 时 Run 挂起（可恢复），不伪造失败让模型继续；
   没有注入 Resolver 时默认挂起，既不自动批准也不自动拒绝。

## 快速开始

```python
from aiharness import (
    FakeProvider, HostBackend, Message, ReadFileTool,
    RuntimeBuilder, Session, SQLiteEventStore,
)

runtime = RuntimeBuilder(
    provider=FakeProvider(),                       # 用哪家模型：应用决定
    sandbox=HostBackend(".", unsafe=True),         # Host 不是隔离边界，必须显式承认
    tools=[ReadFileTool()],                        # 给模型哪些工具：应用决定
).with_artifacts().build()                         # 接线：Harness 负责

store = SQLiteEventStore(".aiharness/events.db")
session = Session.create(store, cwd=".", provider="fake", model="fake-model")
result = await runtime.coordinator.run(
    session, model="fake-model", user_message=Message.text("user", "看看这个仓库")
)
```

`provider`/`sandbox`/`tools` 没有默认值 —— 替你挑这些就是把产品决策塞进库里。
装配（Gateway 重试、Artifact 路径、Hook Bus、子代理接线）由 builder 承担。

应用只能 `from aiharness import ...`：顶层 `__all__`（154 个名字）是唯一受支持的组合面，
子模块路径一律视为内部实现。可运行的完整组合见 [`aicode/`](aicode/README.md)。

## 文档

- [Agent 开发规范](AGENTS.md) —— 项目级约束与安全不变式
- [架构设计](docs/ARCHITECTURE.md) —— 稳定契约：分层、协议、权限、上下文、回放
- [任务分解](docs/TASK.md) —— 里程碑、范围判据与已完成的 Backlog
- [aicode](aicode/README.md) —— 用这套 Harness 组装出的 Coding Agent
- [ADR](docs/adr/) —— 30 篇决策记录；每条安全默认值的变更都在其中留档

## 开发

```bash
python3 -m pytest          # 291 harness + 64 aicode
ruff check .
mypy                       # strict，覆盖 aiharness 与 aicode
```
