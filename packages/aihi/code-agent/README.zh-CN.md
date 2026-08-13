# aihi-code-agent

[English](README.md) | **简体中文**

面向 Coding Agent 的无 UI Python 运行时和 stdio Worker。它位于应用层，组合 `aihi-models` 与
`aihi-agent`，不复制基础 Agent Runtime。

## 功能

- OpenAI、Anthropic、DeepSeek、OpenAI-compatible 和 Fake Provider profile。
- 每个 Provider 支持多个 model catalog，并可通过配置和 CLI 切换。
- Coding 工具、只读 Git 工具、Sandbox、permission mode、Approval 和可恢复 Run。
- 内置及用户/项目 Skill、信任管理和显式 `load_skill`。
- MCP stdio server、受治理 Subagent、Artifact、Context Compaction 和脱敏 `audit.jsonl`。
- 面向本地 CLI 或其他宿主的版本化 JSON-RPC Worker。

## 在架构中的位置

```text
aihi-models → aihi-agent → aihi-code-agent Worker ← @aihi/code-cli
                              ├─ TOML 配置
                              ├─ Coding Prompt / AGENTS.md
                              ├─ tools / Skills / MCP / subagents
                              └─ audit / artifacts / compaction
```

本包无 UI；DTO 和 Schema 位于 [`@aihi/code-protocol`](../code-protocol/README.zh-CN.md)。

## 安装和启动

```bash
uv sync
uv pip install -e packages/aihi/code-agent
aihi-code-agent-worker --help
```

Worker console script 要求 Python 3.11+。

## 配置

用户配置：`~/.aihi/aihi-code.toml`；项目配置：`<workspace>/.aihi/aihi-code.toml`。

```toml
[provider]
name = "openai"
model = "gpt-4.1-mini"
models = ["gpt-4.1-mini", "gpt-4.1"]

[providers.deepseek]
name = "deepseek"
models = ["deepseek-chat", "deepseek-reasoner"]
base_url = "https://api.deepseek.com/chat/completions"

[skills]
load_tool = true

[mcp.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

`model` 缺省时使用 `models` 第一项；旧的单 model 配置仍兼容。ModelRouter、ModelGateway 和
跨 Provider fallback 不在基础包中实现。

## Skill、MCP 与审计

内置 Skill 按包完整性隐式信任；其他作用域需要精确 trust 和 hash。MCP 工具注册后仍经过同一
Policy/Hook/Sandbox 链路。`audit.jsonl` 是脱敏、尽力而为的运维日志，不是运行时事实源。

## 开发和测试

```bash
pytest packages/aihi/code-agent/tests
ruff check packages/aihi/code-agent
mypy packages/aihi/code-agent/src
```

详见 [架构文档](../../../docs/ARCHITECTURE.zh-CN.md) 和 [CLI 文档](../../../apps/aihi-code-cli/README.zh-CN.md)。
