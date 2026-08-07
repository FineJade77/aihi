# aicode

`aicode` 是 Coding Agent 应用层，直接复用 `aiharness` 的 Provider、Tool、Policy、Sandbox、
Runtime 和 Session 实现。应用层只负责配置、Coding Tool 组合、CLI 和后续项目上下文工作流，
不复制 Harness 代码。

所有依赖只能来自 `from aiharness import ...`；深层子模块导入由
`tests/test_import_boundary.py` 阻断。

## 当前骨架

- `src/aicode/config.py`：Provider、模型、workspace、数据库、Artifact/Telemetry 路径和 Host unsafe 配置；
- `src/aicode/prompt.py`：Coding Agent 的系统提示词（产品决策，不进 Harness）；
- `src/aicode/context.py`：把仓库的 `AGENTS.md`/`CLAUDE.md` 作为项目规则注入上下文；
- `src/aicode/app.py`：组装现有 Harness 能力的 `build_runtime`；
- `src/aicode/approvals.py`：终端 Approval UX（Harness 只定义 Resolver 契约）；
- `src/aicode/cli.py`：独立 `aicode` CLI，支持 Fake/真实 Provider 配置和持久化 Session；
- `tests/`：应用层组合契约测试。

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

## 上下文与可观测

每次编译上下文时注入：产品系统提示词 + 仓库规则文件（`AGENTS.md` → `CLAUDE.md` →
`.aicode/rules.md`，取第一个存在的，上限 32 KB，指向工作区外的符号链接一律忽略）+ Skill 索引。

大型工具输出自动外置到 `.aiharness/artifacts/`（可用 `AICODE_ARTIFACTS` 改路径），上下文只留预览和引用。

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

`--accept-edits` 只自动放行工作区编辑；`shell` / `run_tests` 这类执行进程的工具
仍然逐次需要批准。批准一次后，该工具在当前 Run 内不再询问。

`AGENTS.md`/Skill 注入、Memory 和真实 Subagent 工作流仍属于后续 H-02 与应用层任务。

## 查看会话

```bash
aicode sessions                 # 列出持久化的会话
aicode events <session>         # 打印该会话的事件日志（JSON Lines）
```

Harness 本身不提供 CLI —— 它是库，命令行属于应用层。

在仓库根目录进行本地验证：

```bash
PYTHONPATH=src:aicode/src python3 -m pytest aicode/tests
PYTHONPATH=src:aicode/src python3 -m aicode --help
```
