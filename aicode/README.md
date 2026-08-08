# aicode

`aicode` 是 Coding Agent 应用层，直接复用 `aiharness` 的 Provider、Tool、Policy、Sandbox、
Runtime 和 Session 实现。应用层只负责配置、Coding Tool 组合、CLI 和后续项目上下文工作流，
不复制 Harness 代码。

所有依赖只能来自 `from aiharness import ...`；深层子模块导入由
`tests/test_import_boundary.py` 阻断。

## 当前骨架

- `src/aicode/config.py`：分层加载配置（用户 → 项目 → 环境 → 命令行）；
- `src/aicode/project.py`：`~/.aicode` 与 `<workspace>/.aicode` 的布局、字段白名单和凭据存储；
- `src/aicode/prompt.py`：Coding Agent 的系统提示词（产品决策，不进 Harness）；
- `src/aicode/context.py`：把仓库的 `AGENTS.md`/`CLAUDE.md` 作为项目规则注入上下文；
- `src/aicode/app.py`：组装现有 Harness 能力的 `build_runtime`；
- `src/aicode/approvals.py`：一次性 CLI 的 Approval UX（Harness 只定义 Resolver 契约）；
- `src/aicode/tui/`：交互式终端前端（渲染、审批、打断、斜杠命令、首次配置）；
- `src/aicode/cli.py`：独立 `aicode` CLI，支持 Fake/真实 Provider 配置和持久化 Session；
- `tests/`：应用层组合契约测试。

## 安装

装完之后 `aicode` 在 PATH 上，任何目录直接跑，workspace 就是当前目录。

```bash
pip install ./aiharness ./aiharness/aicode      # 两个包必须一起装
aicode                                          # 在任意项目里
```

**必须同时安装两个包。** `aicode` 把依赖钉死成 `aiharness==0.1.0`（不是范围）：
两者一起开发，应用层用的公开 API 还在动，范围会让 pip 配出一个从没被一起测过的组合。

开发时用可编辑安装，改代码立即生效：

```bash
pip install -e ./aiharness -e ./aiharness/aicode
pip install -e './aiharness/aicode[tui]'        # 顺带装上行编辑和历史
```

两个包都带 `py.typed`，所以嵌入 `aiharness` 的项目跑 mypy 能拿到类型。

> **发布前必读**：PyPI 上 `aicode` 这个名字**已被占用**（`aiCode 23.6.22.0`，
> PyPI 会把 `aiCode` 归一化成 `aicode`）。现在 `pip install aicode` 拿到的是别人的包。
> `aiharness` 未被占用。发布前必须改名，否则用户装到的不是这个项目。

## 配置

两个作用域，回答的是不同的问题：

| 位置 | 属于 | 放什么 |
|---|---|---|
| `~/.aicode/config.json` | **你** | 平时用哪家 provider、哪个模型、`format_command` |
| `~/.aicode/credentials.json` | **你** | API key，`0600`，目录 `0700` |
| `<workspace>/.aicode/config.json` | **项目** | 这个 codebase 要用的模型 |
| `<workspace>/.aicode/` | **项目** | `events.db` · `artifacts/` · `history` · `skills/` |

优先级从低到高：内置默认 → 用户配置 → 项目配置 → 环境变量 → 命令行参数。

**API key 永远不写进项目目录。** 项目目录是你会 clone、打包、发给同事的东西，
放进去的凭据迟早会跟着离开。所以它只去 `~/.aicode/credentials.json`，
用 `os.open(..., 0o600)` 创建——不是先写再 chmod，中间那一瞬间也不能是全局可读。

### 项目文件不许说的话

项目配置是**你 clone 来的**，所以它的字段是严格子集。这三项即使写在里面也会被丢弃：

| 字段 | 为什么不行 |
|---|---|
| `unsafe_host` | 承认 Host 没有隔离是操作者的决定，不是仓库的。**用户配置里也不允许**——放弃沙箱应该是一次动作，而不是你某天设过就忘了的开关 |
| `format_command` | 每次编辑后跑一条 shell 命令。Harness 要求这份信任被显式授予，clone 一个仓库不算 |
| `api_key` | 写它直接抛异常 |

`base_url` 允许，但凭据是按 `provider|base_url` 存的——把项目指向另一个端点会去要那个端点的 key，
不会把你原来的悄悄转发过去。

`.aicode/.gitignore` 自动生成：`config.json` 和 `skills/` 该提交，`events.db`、`artifacts/`、`history` 是本地状态。
这条规则由测试用 `git status --ignored` 实测把关，而不是断言文件内容。

### 第一次运行

没有任何配置时，进入聊天窗口后直接问你：

```
  Set up aicode
  workspace  /Users/me/project

  Which model provider?
    1. Claude — api.anthropic.com
    2. OpenAI
    3. Any OpenAI-compatible endpoint
    4. Scripted replies, to try the interface offline
  provider [1]: 1
  model [claude-opus-5]:
  api key (not echoed):

  Save these settings for
    1. every project — /Users/me/.aicode/config.json
    2. this project only — /Users/me/project/.aicode/config.json
  scope [1]: 
```

第一次在任何地方跑 aicode，默认存成**用户配置**——你配置的是 aicode，不是这个仓库。
之后在某个项目里 `/config` 或 `aicode chat --setup` 再问一次，默认就变成项目作用域。

## 交互模式

直接跑 `aicode` 就进入交互会话（等价于 `aicode chat`），workspace 默认是当前目录：

```
  aicode · claude-opus-5
  /Users/me/project
  /help for commands, ctrl-d to leave

› 把 greet 改好看点，然后跑一下

我先看一下文件。

● read_file(app.py)
  ⎿      1	def greet(name):
         2	    return 'hi ' + name
    … +2 lines

● edit_file(app.py)

Approval required edit_file
  --- app.py
  +++ app.py
  -    return 'hi ' + name
  +    return f'hello {name}!'
  reason:  This tool can mutate external state and requires approval.
  sandbox: host (not isolated)
  y=once  a=this tool for the rest of the run  n=deny  s=decide later
  approve? [y/a/n/s] y
  ⎿ Edited app.py; replaced 1 occurrence(s).
```

`aicode run` 保持原样不变——脚本和 CI 要的是 JSON Lines，不是终端 UI。

### 它建在什么之上

整个前端只用了 Harness 的四个公开接缝，**没有为它扩过一次 Harness**：

| 前端能力 | 用的接缝 |
|---|---|
| 流式输出 | `Session.add_event_observer` + `model.chunk` 临时事件（ADR-0021，不落盘） |
| 工具可视化 | `tool.requested` / `tool.started` / `tool.result` 事件 |
| Esc / Ctrl-C 打断 | `RunCoordinator.run(cancel_event=...)` → `run.interrupted` |
| 行内审批 | `ApprovalResolver` 协议 |

因此终端里看到的每一件事都已经在事件日志里，`aicode events <session>` 能原样复现。

### 斜杠命令

| 命令 | 作用 |
|---|---|
| `/help` | 命令列表 |
| `/clear` | 开一个**新** Session。日志只追加，遗忘是换一份日志，旧的仍在盘上 |
| `/config` | 重新问一遍 provider / model / API key，当场重建 runtime |
| `/mode [plan\|default\|accept-edits\|bypass]` | 切换权限档位 |
| `/model [name]` | 切换模型（下一回合生效） |
| `/tools` | 列出模型能调用的工具 |
| `/usage` | 本 Session 的 token 累计 |
| `/session` | Session id、workspace、事件数 |
| `/resume [run_id]` | 继续一个挂起等审批的 Run |
| `/thinking` | 显示/隐藏推理过程 |
| `/exit` | 离开（Ctrl-D 同效） |

斜杠命令改的是**这个终端**的行为，永远不会被当成 Prompt 发给模型。

### 打断

Run 执行期间 Esc 或 Ctrl-C 会设置 `cancel_event`，Harness 在步骤之间检查它并写出 `run.interrupted`，
所以被打断的 Run 仍然可重放。再按一次 Ctrl-C 交还默认处理，不会把人困住。

Esc 需要把终端切到 cbreak，因此只在 POSIX 真 TTY 上启用；条件不满足就静默跳过，
绝不把一个需要 shell 善后的 tty 交出去。审批提问期间会临时把终端还回去（`Interrupts.paused()`）。

### 依赖

**不新增必需依赖**。装了 `prompt_toolkit` 就有历史、行编辑和 `/` 补全，没装就退回 `input()`：

```bash
pip install 'aicode[tui]'
```

流式文本按原样输出、不做 Markdown 重渲染——边流边重绘 Markdown 必须缓冲加重画，
长回复会闪。代价是代码块没有语法高亮。

颜色遵循 `NO_COLOR` / `FORCE_COLOR`，非 TTY 自动关闭；`NO_COLOR` 优先级高于 `FORCE_COLOR`。

## 工具

| 类别 | 工具 | 授权 |
|---|---|---|
| 只读 | `read_file` `glob` `grep` | 免审批，可并行 |
| 写入 | `write_file` `edit_file` | `--accept-edits` 可覆盖 |
| 执行 | `bash` | 永远逐次审批；`--plan` 直接拒绝 |

`bash` 接收命令字符串，管道和 `&&` 正常工作；每次调用是独立的 shell，`cd` 不保留。
查找代码请用 `glob`/`grep` —— 它们不需要审批，不会打断你。

## 模型路由

所有模型请求经过 `ModelGateway`（即使只有一个 Provider）：有界重试 + 请求截止时间，
且只在**首个 stream chunk 之前**才会 Fallback。

`AICODE_SUBAGENT_MODEL` 可以让子代理用更小更便宜的模型；不设置就用主模型。

`AICODE_COMPACT_MODEL` 让长会话的 L2 压缩用专用模型生成结构化摘要；不设置则用离线摘要器，
压缩因此不依赖第二个模型可达。模型失败时自动降级，并在 `compaction.created` 事件里
记为 `l2_model_fallback`。

## 上下文与可观测

每次编译上下文时注入：产品系统提示词 + 仓库规则文件（`AGENTS.md` → `CLAUDE.md` →
`.aicode/rules.md`，取第一个存在的，上限 32 KB，指向工作区外的符号链接一律忽略）+ Skill 索引。

大型工具输出自动外置到 `.aicode/artifacts/`（可用 `AICODE_ARTIFACTS` 改路径），上下文只留预览和引用。

设置 `AICODE_TELEMETRY=<path.jsonl>` 后写出脱敏的 JSON Lines 观测记录；不设置则完全关闭。
`AICODE_PROJECT_RULES=false` 可关闭规则注入。

## Skills

工作区存在 `.aicode/skills/`（或设置 `AICODE_SKILLS`）时，`aicode` 自动把 Skill **索引**
（名称@版本、作用域、一句话描述）注入系统提示词。正文不进上下文，需要时通过 Harness 的
Trust 流程显式加载。

## MCP

`AICODE_MCP` 指向一个声明文件即可接入 MCP 服务器：

```json
{"servers": [{"name": "docs", "command": ["mcp-docs-server"], "allowed_tools": ["search"]}]}
```

只有声明过的服务器会被启动，且只暴露 `allowed_tools` 列出的工具；它们和内置工具走同一条
Policy/Hook/Sandbox 链路。省略 `allowed_tools` 表示接受该服务器当前公布的全部工具。

## Subagent

`AICODE_SUBAGENTS=true` 时注册 `task` 工具，可以把一段只读调查委派给子代理：

- 子代理工作区只读、没有 `process.exec`、`max_depth=1`，因此它不能改文件也不能再派人；
- 子 Run 在自己的 Session 中执行，父侧 Tool Result 记录 `session_id`/`run_id`/`task_id`；
- 派生本身是 mutating 工具：默认模式需要批准，Plan 模式直接拒绝。

## Hook

`AICODE_FORMAT_COMMAND="ruff format"` 会在每次成功的 `write_file`/`edit_file` 之后
对该文件运行格式化命令。

它是**会修改文件的 hook**，因此遵守 Harness 的规矩：注册时必须显式 trust（配置这个命令
就是那次授权），只在外层工具调用已被 Policy 放行、且沙箱已确认时执行，走沙箱（工作区限制、
超时、输出上限），失败只报告不影响 Run。被拒绝的编辑不会被格式化。

## Approval 流程

需要批准的工具不会被伪造成失败，Run 会挂起（退出码 `2`）：

```bash
aicode run "重构这个模块" --unsafe-host          # 挂起并打印 approval_id / run_id
aicode approve <session> <approval_id>          # 或 --deny
aicode resume <session> --run <run_id> --unsafe-host
aicode abandon <session> --run <run_id>          # 放弃这个 Run
```

在终端内直接回答：

```bash
aicode run "重构这个模块" --unsafe-host -i       # y=批准 n=拒绝 其他=挂起
```

`--accept-edits` 只自动放行工作区编辑；`bash` 这类执行进程的工具仍然逐次需要批准。
批准一次后（`a`），该工具在当前 Run 内不再询问。`--plan` 与 `--accept-edits` 互斥：
一个拒绝全部改动，一个预先放行改动，同时给出的是矛盾而不是优先级。

交互模式下直接回答即可，挂起仍然可用（选 `s`）——挂起是写进日志的持久状态，
可以换一个终端用 `aicode approve` / `aicode resume` 接着处理。

## 查看会话

```bash
aicode                          # 进入交互会话
aicode chat --session <id>      # 接着某个会话聊
aicode sessions                 # 列出持久化的会话
aicode events <session>         # 打印该会话的事件日志（JSON Lines）
```

Harness 本身不提供 CLI —— 它是库，命令行属于应用层。

在仓库根目录进行本地验证：

```bash
PYTHONPATH=src:aicode/src python3 -m pytest aicode/tests
PYTHONPATH=src:aicode/src python3 -m aicode --help
```
