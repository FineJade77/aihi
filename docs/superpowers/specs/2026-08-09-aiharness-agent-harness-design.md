# aiharness：不绑厂商的 Agent Harness

日期：2026-08-09
状态：已确认，待转实施计划
关联：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[TASK.md](../../TASK.md)、ADR-0021、ADR-0023、ADR-0027

## 1. 定位

**aiharness 是产品**：一个 harness-only 的库——模型之外的全部（手、眼、记忆、安全边界），
让任何需要「读文件、跑命令、迭代」的 agent 都能建在上面。

**aicode 是参考实现**：一个日常自用的终端 coding agent。它的作用有两个，缺一不可——
逼出 harness API 的真实形状（不好用的接缝会在这里暴露），以及让作者自己每天用到自己的东西。

对标形状是 Claude **Agent SDK**（原名 Claude Code SDK）与 Claude Code 的关系：SDK 是
harness-only 的库，CLI 是建在它上面的另一个产品。改名是因为「不只用来写代码」——官方对 agent
的定义是「planning its own steps and calling tools that read files, run commands, or edit code」，
**任务类型不进定义**。aiharness 采同一条线：Harness 不知道自己在服务什么任务。

### 与 Agent SDK 的厚度差

差异全部落在同一条轴上——可审计、可恢复、可换厂商、可隔离。这不是巧合，是三条不变式的产物。

| 维度 | Agent SDK | aiharness |
|---|---|---|
| Agent loop / 上下文管理 | 有 | 有（`runtime` + `context`，L0/L1/L2 压缩） |
| Hooks / Subagents / MCP / Skills / Memory / Plugins | 有 | 有，一一对应 |
| Sessions（resume / fork） | 有 | 有，且是**事件日志投影**而非状态快照 |
| **模型厂商** | 绑 Anthropic | Provider 协议 + anthropic / openai / openai-compatible + Gateway、角色路由、Fallback |
| **事件日志是事实源** | 无 | 有。schema 版本化、可重放、压缩只追加派生 |
| **沙箱是一等协议** | 无（靠宿主 + 权限） | `SandboxBackend`：Host / Docker / OS-native |
| **审批持久化** | 回调式权限 | Policy 返回 ASK 时 **Run 挂起可恢复**，Approval/Lease 以事件落盘 |
| **Artifact 外置** | 无 | 内容寻址、Retention、审计 |
| **Eval / Replay / Graders** | 无 | `evals` |
| 可观测 | 部分 | `TelemetrySink` + 脱敏 + 成本 |
| 内置工具广度 | 含 web search / fetch | 无 web 类 |
| 语言 | Python + TypeScript | Python |
| 生态与实战检验 | 巨大 | 零 |

更薄的三处（工具广度、TypeScript、生态）里，只有工具广度在本轮范围内。

### 范围边界

| 节点 | 本轮 |
|---|---|
| Python 库 / SDK | 做。这是产品本体 |
| 终端 TUI（aicode） | 做，但优先级在 API 之后 |
| CLI | 做，脚本与 CI 用 JSON Lines |
| HTTP API / 控制面 | 不做。harness-only，部署是使用者的事 |
| Run Coordinator / Runtime / Context Compiler | 已在，不动 |
| Model Gateway + 三个 adapter | 已在。**Gemini 原生 adapter 不写**，走 compatible |
| Tool 链路（校验 → Policy → 审批 → Hook → Sandbox） | 已在，不动。这是三条不变式的载体 |
| Builtin / Plugin / MCP / Memory / Skill | 已在 |
| Subagent Coordinator / Mailbox | **不恢复**。理由见 §2 |
| Event Store → Snapshots | SQLite。**PostgreSQL 不做** |
| OpenTelemetry | 只留 `TelemetrySink` + JSONL。远程 exporter 不做 |
| TypeScript SDK | 不做 |
| 后台任务（`wait=false` 的 task）与 loop 定时器 | 不做，但接口按 §2 预留 |
| web 抓取 / 搜索工具 | 不做。**但列为下一轮第一候选**——通用 harness 里「读官方文档」比 coding-only 时更常见，且它需要 policy 新增一类「出网」判断（现在只管路径和命令），值得单独立项 |

**协议不动**：`EventStore` / `TelemetrySink` / `SandboxBackend` 保留原样。将来真要接控制面
或 Postgres，是新增适配器，不是改 Runtime 契约。

### 需要收回的文档措辞

工作区里未提交的 README / AGENTS.md / TASK.md 改动写的是「支撑多条产品线（Coding + Cowork）」。
方向对，claim 错。正确的说法不是「harness 服务多条产品线」，而是**harness 不知道任务类型**——
前者暗示存在一份产品线清单，后者是无清单的通用性。Cowork 不是第二条产品线，只是又一个消费者。

`aicode/` 的删除保留并提交，应用层按本文重建。

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
- **运行中进度**：消费者订阅子 Session 拿到（§4），Harness 零改动；
- **双向通信**：不做。Mailbox 要有界队列、ack 语义、快照恢复、背压，
  而「子代理中途提问」在单人用的场景里几乎不会发生。

### 现在就要守住的两条约束

即使本轮不做后台任务，这两条也要从第一天守住，否则将来返工：

| 约束 | 为什么 |
|---|---|
| `task` 的 Tool Result 契约是「终态 **或** 引用 + state」 | ADR-0023 已有先例（审批挂起时返回 `state=waiting` + `approval_id`，父 Run 不失败）。将来加 `wait=false` 只是新增一个 state 分支，不是改契约 |
| 子 Run 永远有独立 Session，metadata 保留 parent 链 | 后台任务的可查询性完全建在这上面。一旦允许某条路径复用父 Session，task 就做不成了 |

## 3. API 表面：从 wiring 到一行

这是 harness 作为产品最弱、也最该先补的一处。Agent SDK 的起步是 `query(prompt, options)`；
aiharness 今天的起步是 README 里那段十余行的 `RuntimeBuilder` 装配。`RuntimeBuilder` 本身是对的
（它已经把接线收拢了），缺的是它上面的一层**默认值**。

分两层，边界清晰：

**高层入口**——给「我只想跑起来」的使用者。一次调用完成 Session 创建、Runtime 装配、Run 执行，
返回结果或异步事件流。默认值必须显式而不是隐式：默认工具集是只读的（`read_file` / `glob` /
`grep`），默认 Policy 对写和执行返回 ASK，默认没有 Resolver 时挂起——**默认值可以省事，
不能省安全**。Host 后端仍然必须显式 `unsafe=true`，这一条不因为是高层入口就放宽。

**低层装配**——`RuntimeBuilder` 与全部协议，保持现状不动。高层入口只是它的一种参数组合，
不是另一套实现；任何高层能做的事都必须能用低层原样表达出来。

判据：高层入口一旦出现低层表达不了的能力，说明抽象漏了，要退回改低层。

## 4. 约定式装载

Agent SDK 从 `.claude/` 与 `~/.claude/` 自动装载 skills、commands、memory——**约定放在 SDK 层，
不在应用层**。aiharness 今天有 `SkillDiscovery`、`MemoryService`、MCP 客户端，但没有目录约定，
旧 `aicode/` 里那套 `~/.aicode` + `<ws>/.aicode` 布局是应用层私有的。

本轮把约定上移到 harness：`~/.aiharness/` 与 `<workspace>/.aiharness/`，装载 skills、memory、
项目规则文件、MCP 声明。理由是它属于「任何消费者都需要、且各家实现只会长得一样」的东西——
应用层重复实现一遍没有产品差异，只有不一致风险。

**但三条作用域禁令跟着上移，不放松**：项目作用域的配置是 clone 来的，因此

| 字段 | 项目配置里禁止 | 理由 |
|---|---|---|
| `unsafe_host` | 禁（用户配置里也禁） | 放弃沙箱是一次动作，不是某天设过就忘的开关 |
| `format_command` | 禁 | 每次编辑跑一条 shell 命令，clone 一个仓库不算授予信任 |
| `api_key` | 写它直接抛异常 | 项目目录是会被 clone、打包、发出去的东西 |

凭据只去用户作用域，用 `os.open(..., 0o600)` 创建——不是先写再 chmod，中间那一瞬间也不能是
全局可读。凭据按 `provider|base_url` 存：把项目指向另一个端点会去要那个端点的 key，
不会把原来的悄悄转发过去。

产品默认值（系统提示词、给模型哪些工具、TUI 行为）仍然留在应用层。判据是
「换一个任务领域还成不成立」：目录布局成立，coding 提示词不成立。

## 5. 工具集

沿用：`read_file` `glob` `grep` `write_file` `edit_file` `bash` `task`，加 MCP / plugin 工具。

新增两个，都在 harness 侧：

**`multi_edit`**（`tools/builtin/multi_edit.py`）——一次调用对同一文件做多处替换或跨文件批量改。
与 `edit_file` 同层同规矩：写工具、默认 ASK、外部变更后拒绝盲写。收益是往返轮数、token
和审批次数都降下来。

**`todo`**——模型自维护任务清单。关键设计：**不引入新事件类型，也不开新口子**。
todo 的状态就是最后一次 `todo` 工具调用的 Tool Result 内容，消费者订阅已有的 `tool.result`
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

`ChildRunSubagentRunner` 的 `session_factory` 是注入的。消费者自己包一层工厂，
子 Session 一创建就挂 event observer。因此阻塞版 `task` 跑到一半时，父侧界面照样能实时显示
每个子代理在做什么。这是 ADR-0023 用注入协议保住分层，在这里第一次付息。

## 6. 模型接入

现有 adapter 保持 anthropic / openai / openai-compatible / fake 四个。
DeepSeek、Kimi、GLM、Qwen、Ollama、vLLM、OpenRouter、Gemini 兼容端点全部做成
**配置 preset**（base_url + 默认模型 + capability 开关），是纯数据，不是代码分支。

代价明确：各家非标准扩动（厂商特有的推理字段、提示缓存）拿不到。接受。

preset 放哪一层是个真问题：厂商名字不该进内核（「任何以厂商术语命名的内核字段都是设计错误」）。
结论是 **preset 数据留在应用层**，harness 只认 `base_url` + `Capabilities`。角色路由
（主模型 / 子代理 / 压缩用不同模型）已在 harness 内（`ModelRoles`、`ROLE_*`），
消费者只需在自己的配置面暴露。

## 7. 仓库与包形态

```
aiharness/                        # 仓库根，不变
├── pyproject.toml                # 包 aiharness · 运行时依赖只有 httpx
├── src/aiharness/                # 17 个包 · 分层不动 · 契约测试继续把关
├── code/                         # 参考实现
│   ├── pyproject.toml            # 包 aiharness-code · 依赖 aiharness==0.1.0 + textual
│   ├── src/aiharness_code/
│   └── tests/
├── tests/                        # harness 测试
└── docs/
```

- 分发名 `aiharness-code`，命令 `aicode`，import 名 `aiharness_code`；
- PyPI 上 `aicode` 已被占用（`aiCode 23.6.22.0`，PyPI 会把 `aiCode` 归一化成 `aicode`），
  但**分发名受占用限制、命令名不受**——console script 仍叫 `aicode`；
- 应用包依赖库包，`pip install aiharness-code` 装一次就够；
- 版本钉死不用范围。两者一起改，范围会配出没被一起测过的组合；
- 应用层只能 `from aiharness import ...`，深层子模块导入由契约测试阻断。

Textual / Rich 正好命中 README 已有的拆包判据「需要一个内核不该背的第三方依赖」。
所以双包不是新规矩，是老规矩第一次被触发。

## 8. 参考实现：aicode

```
code/src/aiharness_code/
├── cli.py               入口分发：chat(默认) / run / sessions / events / approve / resume / abandon
├── config/              产品配置面（在 §4 的 harness 约定之上，只加产品字段）
├── providers/presets.py base_url + 默认模型 + capability 开关（纯数据）
├── agent/
│   ├── prompt.py        Coding Agent 系统提示词（产品决策，不进 Harness）
│   ├── tools.py         工具集组装
│   ├── hooks.py         format-on-edit（受治理的 mutating hook）
│   └── build.py         装配
└── tui/
    ├── app.py           Textual App：Run 生命周期、按键、取消
    ├── stream.py        model.chunk → widget 的限速批处理
    ├── commands.py      斜杠命令
    ├── theme.py
    └── widgets/         transcript · message · composer · approval · todo · subagent · status
```

### TUI 布局：主栏 + 常驻右栏

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

理由：边流边重绘 Markdown 必须缓冲加重画，长回复会闪。

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

## 9. 测试

- **Harness 侧**：现有 296 用例不动。新增高层入口的默认值测试（默认只读、写与执行默认 ASK、
  无 Resolver 时挂起、Host 未确认时拒绝执行）、约定装载与作用域禁令、`multi_edit`、
  并行 `task`（`max_children` 上限、能力子集不被并发绕过、一次审批覆盖整批）。
- **应用层**：产品配置分层、凭据权限位（实测 `0600`/`0700`，不是断言代码路径）、
  `.gitignore` 用 `git status --ignored` 实测、import boundary。
- **TUI**：用 Textual 的 `App.run_test()` + `Pilot` 无头驱动按键、断言 widget 状态。

## 10. 交付顺序

harness 先，aicode 验证。每一步都必须是可运行的纵向链路，不留半接的接线。

| | 内容 | 完成时能干什么 |
|---|---|---|
| A | 高层入口 + 安全默认值（§3） | 十行内跑通「读文件 → 跑命令 → 迭代」，不必手写装配 |
| B | 约定式装载 `~/.aiharness/` 与 `<ws>/.aiharness/`（§4） | skills / memory / 规则 / MCP 声明零代码接入 |
| C | `todo` 与 `multi_edit`（§5） | 长任务不跑题；少往返、少审批 |
| D | 并行 `task` 扇出（§5） | 分头调查 |
| E | aicode 骨架：产品配置 · presets · CLI | `aicode run "…"` 跑通完整回路，A–D 第一次被真实消费 |
| F | Textual TUI：transcript · composer · status · 审批 · 流式 · 打断 · todo 与子代理面板 | 能天天用 |
| G | 使用者文档：入门、API 指南、示例（非 coding 的示例至少一个） | 别人能用它搭自己的 agent |
| H | 定位文档重写：README / AGENTS.md / TASK.md | 措辞与代码一致 |

G 里那个非 coding 示例是**验收条件而非装饰**：定位是「harness 不知道任务类型」，
如果没有一个非 coding 的消费者跑通，这句话就没有证据。

需要新写的 ADR 三篇：

1. **高层入口与安全默认值**——默认值省事的边界在哪，为什么 `unsafe=true` 不随之放宽；
2. **约定式装载的作用域禁令**——为什么目录约定属于 harness 而产品默认值不属于；
3. **并行子代理扇出**——ADR-0023 的补充，说明一次审批覆盖整批为什么安全。
