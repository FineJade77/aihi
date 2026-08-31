# @aihi/code-cli

[English](README.md) | **简体中文**

面向 AIHI Coding Agent Worker 的本地 TypeScript/Ink 终端 UI。CLI 启动 Python Worker，完成
Code Protocol `0.3` handshake，渲染 durable/live events，并提供简化 Coding Agent CLI 的交互。

## 功能

- 基于 SQLite EventStore 的 workspace-aware Session。
- New、continue、open、fork、resume、interrupt 和 cancel。
- Provider/Model picker、Approval、Skill/MCP/Tool 查看和 `/doctor` 诊断。
- Content-Length framed JSON-RPC；重连和事件 replay 使用同一 transcript projector。
- 多行输入、命令补全、滚动 transcript、工具预览和凭据脱敏。

## 架构

```text
Ink TUI (React)
      │
RpcClient ── Content-Length JSON-RPC 2.0 ── stdio ── aihi-code-agent Worker
                                                        │
                                                        └─ SQLite events + workspace
```

TUI 只负责展示和交互；Worker 负责配置、Provider/runtime 组合、工具、Skill、MCP、Approval 和
持久化状态。共享 DTO/Schema 位于 [`@aihi/code-protocol`](../../packages/aihi/code-protocol/README.zh-CN.md)。

## 要求

- Node.js 20+
- pnpm 9
- Python 3.11+
- 已安装或可从 workspace 找到 `aihi-code-agent-worker`

## 安装、构建和运行

```bash
pnpm install
pnpm --dir apps/aihi-code-cli build
pnpm --dir apps/aihi-code-cli start -- --workspace /path/to/project
```

Python Worker 仅在 workspace 内使用。上面的 `uv sync` 会安装它；要在另一个本地 Python 环境中
暴露 Worker，执行：

```bash
uv pip install -e packages/aihi/code-agent
```

它不发布到 PyPI；公开 Python distribution 只有 `aihi-models` 与 `aihi-agent`。

常用参数包括 `--workspace`/`--cwd`、`--session`、`--model` 和 `--provider`。配置文件路径固定为用户
`~/.aihi/aihi-code.toml` 与项目
`<workspace>/.aihi/aihi-code.toml`，不能通过 CLI 改变配置目录。

`[agent]` 使用 `access_mode = "read_only" | "workspace_write" | "full_access"` 与
`run_mode = "execute" | "plan"`。Workspace 不在 TOML 中配置，而是 Session 创建时传入的 canonical cwd。

## 使用示例

CLI 展示流式进度、Tool Result、Skill 加载和可恢复的 Session Header，运行时状态由 Python Worker 负责：

![AIHI Code CLI 交互式会话](docs/aihi-code-cli-session.png)

## Slash 命令

常用命令：`/help`、`/new`、`/open`、`/sessions`、`/resume`、`/cancel`、`/providers`、
`/models`、`/status`、`/doctor`、`/config`、`/skills`、`/mcp`、`/tools`、`/approvals`、`/quit`。

## Session 与安全

Worker 是 EventStore 唯一写入端；TUI 重连时先分页 replay，再消费实时通知。`/doctor` 检查
Session-scoped 状态、Worker、配置、MCP、Skill 和 audit JSONL 目标。Host 执行必须显式
`unsafe=true`，CLI 不绕过 Policy 或命令 Sandbox。Host 不是进程或文件系统隔离：命令以 Workspace
作为 cwd、使用本地用户权限执行。启动页、`/config`、`/status`、`/doctor`、`/runs` 和状态栏显示实际
生效的 Access/Run Mode；存在 Run 时以其持久化 Profile 为准。

## 开发

```bash
pnpm --dir apps/aihi-code-cli typecheck
pnpm --dir apps/aihi-code-cli test
```

参见 [架构文档](../../docs/ARCHITECTURE.zh-CN.md) 和 [Coding Agent 文档](../../packages/aihi/code-agent/README.zh-CN.md)。
