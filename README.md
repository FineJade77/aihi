# aiharness

**Agent Harness 基础设施**——模型之外的全部：手、眼、记忆和安全边界，做成一个可嵌入的库。

一套底座，支撑多条 Agent 产品线。`aicode`（终端 Coding Agent）是第一条，不是唯一一条。

不是 SDK：SDK 意味着背后有一个平台。这里没有，也不绑任何厂商——`Provider` 协议加
anthropic / openai / openai-compatible 适配器。也不是编排框架：核心难点在上下文管理、
工具执行安全、会话可恢复，而不是编排抽象的可插拔性。

## 状态

M0–M7 与 H-01 ~ H-14 全部完成。约 16.9k 行，19 个包，运行时依赖只有 `httpx`。

`aiharness` 是**库**，不带 CLI、不带 HTTP 服务。命令行、Prompt 和产品默认值属于 `aicode/`
这样的应用层。平台类能力（控制面、多 Worker、PostgreSQL、远程 OTel）**刻意不做**，
判据与已移除清单见 [TASK.md](docs/TASK.md#范围不做平台增强)。

本地默认执行后端为 Host。由于 Host 不能提供真正的系统隔离，启用时必须显式传入
`unsafe=true`，并把该事实写入运行事件。Docker 与 OS-native 隔离是可选后端。

## 分层

支撑多条 Agent 产品线的基础设施，形状由真实 import 图决定，不是画出来的：

```
主干（严格有序，只能向下依赖）
  core · artifacts                       零内部依赖
  hooks · observability · models · sandbox
  policy · sessions · context
  tools
  runtime                                ★ 不 import 任何能力包

能力层  mcp · skills · memory · agents · plugins
        只依赖主干到 tools 为止，从不碰 runtime，因此可增可换可去

组合层  builder · evals · __init__
        唯一知道全部的地方——这是 builder 的职责，也是别处不需要知道的原因

应用层  aicode/ 这样的产品：Prompt · 工具选择 · CLI · 终端 TUI · 审批交互
```

★ 这条不变式由 [`tests/contract/test_layering.py`](tests/contract/test_layering.py) 强制执行。
`runtime` 一旦 import 能力包，或能力包一旦 import `runtime`，构建当场失败。
新增顶层包必须显式归层，不能默认继承。

内核只表达**意图**，厂商机制留在适配器。任何以厂商术语命名的内核字段都是设计错误。

### 为什么是一个发行包，不是 `aiharness-*` 多个

拆发行包的标准理由是**依赖隔离**——让不用某功能的人不背它的依赖。这里没有可隔离的东西：
19 个包里 18 个纯 stdlib，唯一的第三方依赖是 `models` 用的 `httpx`。
而多包的成本是实的：一个没人测过的版本矩阵。

判据（满足任一条才独立发行）：

1. 它需要一个内核不该背的第三方依赖（例如 Docker SDK、厂商 SDK）；
2. 它能脱离内核单独使用。

今天没有任何包满足。「不做平台增强」的范围决定已经砍掉了主要候选（PostgreSQL、远程 OTel）。
分层的价值来自被强制执行的边界，而不是 metadata 里的包名——边界在了，将来要拆随时能拆。

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

应用只能 `from aiharness import ...`：顶层 `__all__`（156 个名字）是唯一受支持的组合面，
子模块路径一律视为内部实现。可运行的完整组合见 [`aicode/`](aicode/README.md)。

## 文档

- [Agent 开发规范](AGENTS.md) —— 项目级约束与安全不变式
- [架构设计](docs/ARCHITECTURE.md) —— 稳定契约：分层、协议、权限、上下文、回放
- [任务分解](docs/TASK.md) —— 里程碑、范围判据与已完成的 Backlog
- [aicode](aicode/README.md) —— 用这套 Harness 组装出的 Coding Agent
- [ADR](docs/adr/) —— 30 篇决策记录；每条安全默认值的变更都在其中留档

## 安装

```bash
pip install -e . -e aicode/     # 两个包一起装；aicode 把 aiharness 钉死到同版本
aicode                          # 任意目录，workspace 就是当前目录
```

## 开发

```bash
python3 -m pytest          # 296 harness + 93 aicode
ruff check .
mypy                       # strict，覆盖 aiharness 与 aicode
```
