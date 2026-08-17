# aihi-code-agent

[English](README.md) | **简体中文**

面向 AIHI 的无 UI Coding Agent Runtime 和 stdio Worker。

`aihi-code-agent` 是应用层包，负责将 `aihi-models` 与 `aihi-agent` 组合为 Coding 工作流。它拥有配置、
Coding Prompt、Workspace Tool、Skill/MCP 集成、Subagent、审计接线和 Worker 入口。TypeScript
TUI 位于独立包中。

## 功能

- 支持 OpenAI、Anthropic、DeepSeek、OpenAI-compatible endpoint 和确定性的 Fake Provider profile。
- 提供 Coding Tool、只读 Git Tool、Sandbox 选择、permission mode、Approval 和可恢复 Run。
- 支持内置及用户/项目 Skill、信任管理和显式 `load_skill` Tool。
- 支持 MCP stdio Server、受治理 Subagent、Artifact、Context Compaction 和脱敏 `audit.jsonl` 观测。
- 为本地 CLI 或其他宿主提供版本化 JSON-RPC Worker transport。

## 在架构中的位置

```text
aihi-models  →  aihi-agent  →  aihi-code-agent Worker  ←  @aihi/code-cli
                                  │
                                  ├── TOML 配置
                                  ├── Coding Prompt 和 AGENTS.md
                                  ├── Tool / Skill / MCP / Subagent
                                  └── audit、Artifact、Compaction
```

本包无 UI。共享 DTO 和 Schema 边界位于
[`@aihi/code-protocol`](../code-protocol/README.zh-CN.md)；Ink 终端应用位于
[`apps/aihi-code-cli`](../../../apps/aihi-code-cli/README.zh-CN.md)。

## 安装

已发布版本：

```bash
python -m pip install aihi-code-agent==0.1.0
```

参见 [PyPI 项目页](https://pypi.org/project/aihi-code-agent/0.1.0/)。该命令会自动安装兼容的
`aihi-agent` 和 `aihi-models` 依赖。仓库开发使用：

在仓库根目录执行：

```bash
uv sync
```

将 Worker 安装到已有 Python 环境：

```bash
uv pip install -e packages/aihi/code-agent
```

本包要求 Python 3.11+，并提供 `aihi-code-agent-worker` console script。

## 启动 Worker

```bash
python -m aihi.code_agent.worker
# 或安装后：
aihi-code-agent-worker
```

Worker 通过 stdin/stdout 读写使用 Content-Length framing 的 JSON-RPC 2.0 消息。Protocol 版本
`0.2` 通过精确版本 handshake 协商。`run.start` 和 `run.resume` 以异步方式接受；进度和终态
通过 notification 发送，例如 `run.completed`、`run.failed`、`run.interrupted`、
`run.cancelled` 和 `approval.requested`。

通常由 CLI 启动 Worker；也可以将它嵌入实现同一协议的其他本地宿主之后。

## 使用示例

Worker 可以驱动交互式 CLI 会话，展示流式进度、Tool Result、Skill 加载以及可恢复的 Session Header：

![AIHI Code Agent 交互式会话](docs/aihi-code-agent-session.png)

## 配置

配置路径是有意固定的，不支持通过命令行或环境变量覆盖配置目录。配置文件按从低到高的优先级合并：

1. `~/.aihi/aihi-code.toml`
2. 旧版项目根目录 `aihi-code.toml`
3. `<workspace>/.aihi/aihi-code.toml`

相对路径相对于声明它的 TOML 文件解析。生成的用户配置默认将审计输出写入
`~/.aihi/audit.jsonl`；项目配置默认写入 `<workspace>/.aihi/audit.jsonl`。

最小示例：

```toml
[provider]
name = "deepseek"
models = ["deepseek-chat", "deepseek-reasoner"]
api_key_env = "DEEPSEEK_API_KEY"

# 每个 Provider profile 都有自己的 Model catalog。model 可选；
# 缺省使用 models 的第一项（兼容旧的单 Model 配置）。
[providers.local]
name = "openai-compatible"
models = ["local-model", "local-fast"]
model = "local-model"
base_url = "http://127.0.0.1:8000/v1/chat/completions"
api_key_env = "LOCAL_API_KEY"

[sandbox]
backend = "docker"
root = "."

[agent]
permission_mode = "default" # default | accept_edits | plan | bypass

[audit]
enabled = true
path = "audit.jsonl"

[[skills.roots]]
path = "~/.aihi/skills"
scope = "user"

[skills]
load_tool = true

[mcp.servers.example]
command = "npx"
args = ["-y", "some-mcp-server"]
```

API Key 只放在环境变量中；`config.get` 只向外暴露非敏感 metadata。`permission_mode` 会和
Run startup configuration 一起持久化。任何 mode 下硬安全拒绝都保持生效。Host 执行采用 fail-closed
策略，且不是隔离边界；交互式确认会按精确的 Workspace/root 写入 `~/.aihi/host-workspaces.json`。

每个 Provider 可以通过 `models = [...]` 暴露多个 Model。Provider 的 active/default Model 是
`model` 指定的值，或 catalog 第一项。Model 只有在声明它的 Provider 下才有效；`config.get`
向客户端返回不含 Secret 的 Provider/Model catalog。

## Skill 与 Subagent

内置 Skill（`code_review`、`debug`、`refactor`、`test_writing`）属于包内容，默认隐式信任。
用户、项目和 Workspace Skill 在加载前必须显式 trust。只有 load Tool 可用时才向模型发送 Skill
索引；调用 `load_skill` 时使用裸 Skill name（例如 `code_review`），不要使用带版本后缀的展示名称。

命名 Subagent 通过 `task` Tool 选择：`explore`、`code_review`、`test` 和 `general`。默认配置将
Subagent 限制为深度 1、最多 3 个子 Agent 和只读文件系统能力。必须使用 Worker Session Store，才能
将父 Run 与子 Run 一起 Replay。

## 审计与运行行为

每个 Run 默认向 `audit.jsonl` 输出脱敏、有界的观测。文件以仅 Owner 可读写的权限（`0600`）创建；
`/doctor` 检查文件和父目录的可写性，但不会为了检查而创建缺失的审计文件。可以通过
`[audit] enabled = false` 禁用 Sink，或设置相对于声明它的 TOML 文件的路径。

Tool Call 在执行前持久化，并且恰好返回一个 Result。Approval resolution 与 Resume 是两个独立操作：
客户端先解决 Approval，再调用 `run.resume`。这样可以保持 Worker Protocol 的确定性，同时让 TUI
把两个操作呈现为一个交互。

## 开发与测试

```bash
uv run pytest packages/aihi/code-agent/tests
uv run ruff check packages/aihi/code-agent
uv run mypy
uv run python -m build --wheel --no-isolation packages/aihi/code-agent
```

完整 workspace 检查见[仓库 README](../../../README.zh-CN.md)。Worker Protocol 契约与
[`@aihi/code-protocol`](../code-protocol/README.zh-CN.md) 一起测试。

## 安全边界

- 将模型输出、Tool 输入、MCP 响应、Skill 和 Subagent 输出视为不可信输入。
- 凭据放在环境变量或外部 Secret Manager 中，不要写入 TOML 或 Event 内容。
- 不要把 `HostBackend` 当作 Sandbox；需要进程隔离时选择隔离 backend。
- 保持有限的 turn limit，审查 `permission_mode`，启用不安全的本地执行前必须获得显式 Host 确认。

## 相关文档

- [Agent 基础包](../agent/README.zh-CN.md)
- [Worker Protocol](../code-protocol/README.zh-CN.md)
- [CLI/TUI](../../../apps/aihi-code-cli/README.zh-CN.md)
- [仓库架构](../../../docs/ARCHITECTURE.zh-CN.md)
