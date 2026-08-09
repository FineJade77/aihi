# aicode：终端 Coding Agent 设计

日期：2026-08-09
状态：已确认，待转实施计划
关联：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[TASK.md](../../TASK.md)、ADR-0021、ADR-0023、ADR-0027

## 1. 定位与边界

**产品是 `aicode`**：一个自用为主的全屏终端 coding agent。三个特征决定它的取舍——
不绑厂商、全部可改可控、不追求在工作流上赢过 Claude Code。

**`aiharness` 是它的基础设施**，同时作为独立库可用。但库的形态从此是「顺带」，不是目标：
凡是只服务于假想外部用户的抽象，不做。

这次调整推翻了 2026-08-08 那版「支撑多条产品线（Coding + Cowork）的 Harness、
本仓库不含应用层」的定位。README / AGENTS.md / TASK.md 中相应措辞需要重写。
工作区里 `aicode/` 的删除保留并提交——应用层按本文重写，不恢复旧代码。

### 架构节点定生死

| 节点 | 本轮 |
|---|---|
| CLI / TUI / Python SDK | 做。TUI 是主形态，CLI 保留脚本入口，SDK 已在 |
| HTTP API / 控制面 | 不做。自用不需要第二台机器 |
| Session API 门面 | 不新建抽象层。现有 `Session` + `RunCoordinator` 就是它 |
| Run Coordinator / Runtime / Context Compiler | 已在，不动 |
| Model Gateway + Anthropic / OpenAI / Compatible | 已在。**Gemini 原生 adapter 不写**，走 compatible |
| Tool 链路（校验 → Policy → 审批 → Hook → Sandbox） | 已在，不动。这是三条不变式的载体 |
| Builtin / Plugin / MCP | 已在 |
| Subagent Coordinator / Mailbox | **不恢复**。理由见 §2 |
| Memory / Skill Registry | 已在 |
| Event Store → Snapshots | SQLite。**PostgreSQL 不做** |
| Artifact Store | 已在 |
| OpenTelemetry | 只留 `TelemetrySink` + JSONL。远程 exporter 不做 |

一并出局：Cowork / 多人协作、Web 与桌面前端、多 Worker、web 抓取工具、目录树工具、
后台任务（`wait=false` 的 task）与 loop 定时器。

**协议不动**：`EventStore` / `TelemetrySink` / `SandboxBackend` 保留原样。将来真要接控制面
或 Postgres，是新增适配器，不是改 Runtime 契约。

## 2. Subagent：为什么不恢复 Coordinator

图中 `Subagent Coordinator → Run Coordinator` 那条边有三个问题，都不是风格问题。

**其一，安全一致性会失去。** 三条不变式之一是「所有副作用经过
`tools → policy → hooks → sandbox`」。派生一个能读整个仓库的子 Run 是副作用里最重的一种。
Coordinator 路线要在 Runtime 里把审批、审计、取消重写一份——那是同一件事的第二个实现，
两份实现迟早不一致，而不一致的那一侧就是提权口子。

**其二，分层不变式会破。** `runtime` 不 import 能力包、能力包不碰 `runtime`，这条由
`tests/contract/test_layering.py` 编译期强制。`agents → runtime` 那条边加上去，构建当场失败。
现状之所以成立，是因为 `ChildRunSubagentRunner` 拿到的是注入的
`CoordinatorFactory` 协议，`agents/` 一行都没 import `runtime/`。

**其三，对未来的后台任务反而是负担。** 后台任务需要的是持久化的、可查询的、能跨进程恢复的
运行记录，而不是内存里的编排。ADR-0023 已经把这个底座建好了——子 Run 拥有独立 Session，
metadata 记录 `parent_session_id` / `parent_run_id` / `task_id` / `depth`，
所以「有哪些任务、跑到哪、输出是什么」本来就在盘上。Coordinator 要做后台任务，
得再维护一份自己的快照，而那份快照是事件日志的重复投影，直接违反第一条不变式。

Coordinator 确实赢在三点：并行扇出、运行中进度可见、父子双向通信。本轮的应对是：

- **并行扇出**：给 `task` 工具加，不需要 Coordinator（§4）；
- **运行中进度**：TUI 订阅子 Session 拿到（§4），Harness 零改动；
- **双向通信**：不做。Mailbox 要有界队列、ack 语义、快照恢复、背压，
  而「子代理中途提问」在单人用的 coding agent 里几乎不会发生。

### 现在就要守住的两条约束

即使本轮不做后台任务，这两条也要从第一天守住，否则将来返工：

| 约束 | 为什么 |
|---|---|
| `task` 的 Tool Result 契约是「终态 **或** 引用 + state」 | ADR-0023 已有先例（审批挂起时返回 `state=waiting` + `approval_id`，父 Run 不失败）。将来加 `wait=false` 只是新增一个 state 分支，不是改契约 |
| 子 Run 永远有独立 Session，metadata 保留 parent 链 | 后台任务的可查询性完全建在这上面。一旦允许某条路径复用父 Session，task 就做不成了 |

## 3. 仓库与包形态

```
aiharness/                        # 仓库根，不变
├── pyproject.toml                # 包 aiharness · 运行时依赖只有 httpx
├── src/aiharness/                # 17 个包 · 分层不动 · 契约测试继续把关
├── code/                         # 新应用层
│   ├── pyproject.toml            # 包 aiharness-code · 依赖 aiharness==0.1.0 + textual
│   ├── src/aiharness_code/
│   └── tests/
├── tests/                        # harness 测试
└── docs/
```

- 分发名 `aiharness-code`，命令 `aicode`，import 名 `aiharness_code`；
- PyPI 上 `aicode` 已被占用（`aiCode 23.6.22.0`，PyPI 会把 `aiCode` 归一化成 `aicode`），
  但**分发名受占用限制、命令名不受**——console script 仍叫 `aicode`；
- 应用包依赖库包，`pip install aiharness-code` 装一次就够，不再是旧版「必须同时装两个」；
- 版本钉死不用范围。两者一起改，范围会配出没被一起测过的组合；
- 应用层只能 `from aiharness import ...`，深层子模块导入由契约测试阻断。

Textual / Rich 正好命中 README 已有的拆包判据「需要一个内核不该背的第三方依赖」。
所以双包不是新规矩，是老规矩第一次被触发。

## 4. 模型接入与工具集

### 模型：靠 compatible + preset，不写新 adapter

现有 adapter 保持 anthropic / openai / openai-compatible / fake 四个。
DeepSeek、Kimi、GLM、Qwen、Ollama、vLLM、OpenRouter、Gemini 兼容端点全部做成
**配置 preset**（base_url + 默认模型 + capability 开关），是纯数据，不是代码分支。

代价明确：各家非标准扩动（厂商特有的推理字段、提示缓存）拿不到。接受。

角色路由（主模型 / 子代理 / 压缩用不同模型）已在 Harness 内（`ModelRoles`、`ROLE_*`），
应用层只需在配置面暴露。

### 工具集

沿用：`read_file` `glob` `grep` `write_file` `edit_file` `bash` `task`，加 MCP / plugin 工具。

新增两个，都在 Harness 侧：

**`multi_edit`**（`tools/builtin/multi_edit.py`）——一次调用对同一文件做多处替换或跨文件批量改。
与 `edit_file` 同层同规矩：写工具、默认 ASK、外部变更后拒绝盲写。收益是往返轮数、token
和审批次数都降下来。

**`todo`**——模型自维护任务清单。关键设计：**不引入新事件类型，也不开新口子**。
todo 的状态就是最后一次 `todo` 工具调用的 Tool Result 内容，TUI 订阅已有的 `tool.result`
事件解析即可。这天然满足第一条不变式（状态从事件投影而来），并且绕开了 ADR-0023
「未采纳」里明确拒绝过的东西——给工具一个通用事件 sink。

### 并行子代理扇出

`agents/subagent.py` 的 `task` schema 从单个 `objective` 改成接受一组，内部
`asyncio.gather` 并发跑子 Run。`TaskGraph.max_children`（`graph.py:173`）因此第一次真正生效。

- 仍是**一次工具调用**，所以仍走 `tools → policy → hooks → sandbox`，审批只问一次；
- 能力子集、budget 子集、workspace 包含、深度上限的校验对每个子任务逐一执行，
  并发不构成绕过路径；
- `mutates=True` 不变，Plan 模式仍然直接拒绝派生；
- 子代理默认仍继承父能力集合**减去** `agent.spawn`。

需要一篇 ADR 说明「一次审批覆盖整批为什么安全」。

### 子代理进度：Harness 零改动

`ChildRunSubagentRunner` 的 `session_factory` 是注入的。应用层自己包一层工厂，
子 Session 一创建就交给 TUI 挂 event observer。因此阻塞版 `task` 跑到一半时，
父侧界面照样能实时显示每个子代理在做什么。这是 ADR-0023 用注入协议保住分层，在这里第一次付息。

## 5. 应用层内部结构

```
code/src/aiharness_code/
├── cli.py               入口分发：chat(默认) / run / sessions / events / approve / resume / abandon
├── config/
│   ├── schema.py        配置数据类 + 作用域白名单
│   ├── layers.py        内置默认 → 用户 → 项目 → 环境 → 命令行
│   ├── paths.py         ~/.aicode 与 <ws>/.aicode 布局、.gitignore 生成
│   └── credentials.py   按 provider|base_url 存 key，os.open(0o600)
├── providers/presets.py base_url + 默认模型 + capability 开关（纯数据）
├── agent/
│   ├── prompt.py        系统提示词（产品决策，不进 Harness）
│   ├── rules.py         AGENTS.md → CLAUDE.md → .aicode/rules.md，32KB 上限，越界符号链接忽略
│   ├── tools.py         工具集组装
│   ├── hooks.py         format-on-edit（受治理的 mutating hook）
│   ├── mcp.py           MCP 声明加载
│   └── build.py         RuntimeBuilder 组装
└── tui/
    ├── app.py           Textual App：Run 生命周期、按键、取消
    ├── stream.py        model.chunk → widget 的限速批处理
    ├── commands.py      斜杠命令
    ├── theme.py
    └── widgets/         transcript · message · composer · approval · todo · subagent · status
```

没有第二个 553 行的 `cli.py`——每个文件一个职责。

### 配置面统一

旧版把能力散在 `AICODE_MCP` / `AICODE_SUBAGENTS` / `AICODE_SKILLS` / `AICODE_FORMAT_COMMAND` /
`AICODE_TELEMETRY` / `AICODE_SUBAGENT_MODEL` / `AICODE_COMPACT_MODEL` 一堆环境变量里。
新版全部收进配置文件的具名字段，**环境变量降级为只做覆盖**，不再是唯一开关。

两个作用域回答不同的问题：

| 位置 | 属于 | 放什么 |
|---|---|---|
| `~/.aicode/config.json` | 你 | 平时用哪家 provider、哪个模型、`format_command` |
| `~/.aicode/credentials.json` | 你 | API key，`0600`，目录 `0700` |
| `<ws>/.aicode/config.json` | 项目 | 这个 codebase 要用的模型 |
| `<ws>/.aicode/` | 项目 | `events.db` · `artifacts/` · `history` · `skills/` |

优先级：内置默认 → 用户配置 → 项目配置 → 环境变量 → 命令行参数。

**API key 永远不写进项目目录**——项目目录是会被 clone、打包、发给别人的东西。
凭据只去 `~/.aicode/credentials.json`，用 `os.open(..., 0o600)` 创建，
不是先写再 chmod（中间那一瞬间也不能是全局可读）。

项目配置是 clone 来的，所以字段是严格子集。这三项即使写在里面也丢弃：

| 字段 | 为什么 |
|---|---|
| `unsafe_host` | 承认 Host 没有隔离是操作者的决定，不是仓库的。**用户配置里也不允许**——放弃沙箱应该是一次动作，不是某天设过就忘的开关 |
| `format_command` | 每次编辑跑一条 shell 命令。这份信任必须显式授予，clone 一个仓库不算 |
| `api_key` | 写它直接抛异常 |

`base_url` 允许，但凭据按 `provider|base_url` 存——把项目指向另一个端点会去要那个端点的 key，
不会把原来的悄悄转发过去。

`.aicode/.gitignore` 自动生成：`config.json` 和 `skills/` 该提交，
`events.db`、`artifacts/`、`history` 是本地状态。这条规则由测试用 `git status --ignored`
实测把关，而不是断言文件内容。

## 6. TUI

### 布局：主栏 + 常驻右栏

```
┌────────────────────────────────┬──────────────────────┐
│ › 重构 auth 模块                │ TODO                 │
│                                │  ✓ 读 auth/          │
│ 我先把入口找出来。               │  ▶ 拆 session.py     │
│                                │  ○ 更新测试           │
│ ● grep "def login"             │                      │
│   auth/session.py:42           │ 子代理 2              │
│                                │  ▶ token 刷新逻辑     │
│ ● edit_file auth/session.py    │    grep… 8 个文件     │
│   - def login(u, p):           │  ▶ 中间件链           │
│   + def login(user, pw):       │    read middleware.py│
│                                │                      │
│ ┌ 需要批准 · edit_file ──────┐  │ 本轮改动              │
│ │ sandbox host（无隔离）      │  │  M auth/session.py   │
│ │ y 本次  a 本轮  n 拒  s 后  │  │                      │
│ └───────────────────────────┘  │                      │
├────────────────────────────────┴──────────────────────┤
│ › ▁                                                   │
├───────────────────────────────────────────────────────┤
│ claude-opus-5 · default · 12.4k tok · ~/project        │
└───────────────────────────────────────────────────────┘
```

右栏内容可切换（todo / 子代理 / 本轮改动）。终端窄于 100 列时自动收成单栏。
选常驻栏而非召唤式面板的理由：并行扇出是本轮特意加的能力，没有常驻位置它就看不见。

### 流式渲染

`model.chunk` 是临时事件（ADR-0021，不落盘）。这是选 Textual 后唯一的性能敏感点：

- 流式进行时，助手消息以**纯文本**追加，按约 30ms 合帧，不做 Markdown 重渲染；
- 消息终结时，一次性换成渲染后的 Markdown widget（代码块高亮、表格、列表）；
- 工具输出（diff、代码片段）不流式，到达即渲染。

理由与旧版一致：边流边重绘 Markdown 必须缓冲加重画，长回复会闪。
区别是旧版从头到尾不渲染，新版只是**推迟**渲染。

### 打断与审批

Esc 或 ctrl-c 设置 `cancel_event`，Harness 在步骤之间检查并写出 `run.interrupted`，
被打断的 Run 仍可重放。Textual 全程持有终端，所以旧版为 Esc 手动切 cbreak、
审批时再还回去的那套代码整个不用写。

审批是行内 widget：`y` 本次 / `a` 本轮该工具 / `n` 拒绝 / `s` 稍后。
`s` 是持久挂起，可以换个终端用 `aicode approve` 接着处理——审批不是 UI 状态，是写进日志的事实。

### 斜杠命令

改的是这个终端的行为，永远不作为 Prompt 发给模型。

| 命令 | 作用 |
|---|---|
| `/help` `/exit` `/clear` | 帮助 / 离开 / 开新 Session（遗忘是换一份日志，旧的仍在盘上） |
| `/config` `/provider` `/model` | 重配 / 切 preset / 切模型 |
| `/mode [plan\|default\|accept-edits\|bypass]` | 权限档位 |
| `/tools` `/usage` `/session` | 工具集 / token 累计 / 会话信息 |
| `/resume [run_id]` | 继续挂起等审批的 Run |
| `/panel [todo\|agents\|diff\|off]` | 切右栏内容 |
| `/thinking` | 显示 / 隐藏推理过程 |

## 7. 测试

- **Harness 侧**：现有 296 用例不动。新增 `multi_edit` 与并行 `task` 的用例，
  重点是 `max_children` 上限、能力子集不被并发绕过、一次审批覆盖整批。
- **应用层**：配置分层与作用域白名单、凭据权限位（实测 `0600`/`0700`，
  不是断言代码路径）、`.gitignore` 用 `git status --ignored` 实测、import boundary。
- **TUI**：用 Textual 的 `App.run_test()` + `Pilot` 无头驱动按键、断言 widget 状态。

## 8. 交付顺序

| | 内容 | 完成时能干什么 |
|---|---|---|
| A | 包骨架 · 配置面 · presets · `build_runtime` · 一次性 CLI | `aicode run "…"` 跑通完整回路 |
| B | Textual TUI 最小可用：transcript · composer · status · 审批 · 流式 · 打断 | 能天天用 |
| C | `todo` 工具 + 右栏 todo 面板 | 长任务不跑题 |
| D | `multi_edit` | 少往返、少审批 |
| E | 并行 `task` 扇出 + 子代理面板 | 分头调查 |
| F | MCP · skills · format hook · 角色路由 | 能力补齐 |
| G | 文档重写：README / AGENTS.md / TASK.md | 定位与代码一致 |

每一步都必须是可运行的纵向链路，不留半接的接线。

需要新写的 ADR 两篇：

1. **并行子代理扇出**——ADR-0023 的补充，说明一次审批覆盖整批为什么安全；
2. **应用层包边界**——为什么这次触发了拆包判据，以及边界靠什么强制。
