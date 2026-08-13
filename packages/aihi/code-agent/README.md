# aihi-code-agent

[English] | [简体中文](README.zh-CN.md)

UI-free coding-agent runtime and stdio Worker for AIHI.

`aihi-code-agent` is the application layer that composes `aihi-models` and `aihi-agent` into a coding workflow. It owns configuration, coding prompts, workspace tools, Skills/MCP integration, subagents, audit wiring, and the Worker entrypoint. The TypeScript TUI is a separate package.

## Features

- Provider profiles for OpenAI, Anthropic, DeepSeek, OpenAI-compatible endpoints, and the deterministic fake provider.
- Coding tools, read-only Git tools, sandbox selection, permission modes, approvals, and resumable runs.
- Built-in and user/project Skills with trust management and an explicit `load_skill` tool.
- MCP stdio servers, governed subagents, artifacts, context compaction, and redacted `audit.jsonl` observations.
- Versioned JSON-RPC Worker transport for a local CLI or another host application.

## Position in the stack

```text
aihi-models  →  aihi-agent  →  aihi-code-agent Worker  ←  @aihi/code-cli
                                  │
                                  ├── TOML configuration
                                  ├── coding prompts and AGENTS.md
                                  ├── tools / Skills / MCP / subagents
                                  └── audit, artifacts, compaction
```

This package is UI-free. The shared DTO and schema boundary lives in [`@aihi/code-protocol`](../code-protocol/README.md); the Ink terminal application lives in [`apps/aihi-code-cli`](../../../apps/aihi-code-cli/README.md).

## Installation

From the repository root:

```bash
uv sync
```

To install the Worker into an existing Python environment:

```bash
uv pip install -e packages/aihi/code-agent
```

The package requires Python 3.11+ and publishes the `aihi-code-agent-worker` console script.

## Start the Worker

```bash
python -m aihi.code_agent.worker
# or, after installation:
aihi-code-agent-worker
```

The Worker reads and writes Content-Length framed JSON-RPC 2.0 messages on stdin/stdout. Protocol version `0.2` is negotiated by an exact-version handshake. `run.start` and `run.resume` are accepted asynchronously; progress and terminal state arrive as notifications such as `run.completed`, `run.failed`, `run.interrupted`, `run.cancelled`, and `approval.requested`.

The Worker is normally launched by the CLI. It can also be embedded behind another local host that implements the same protocol.

## Configuration

Configuration paths are fixed by design; there is no command-line or environment-variable override for the config directory. Files are merged from low to high precedence:

1. `~/.aihi/aihi-code.toml`
2. legacy project-root `aihi-code.toml`
3. `<workspace>/.aihi/aihi-code.toml`

Relative paths are resolved relative to the file that declares them. The generated user configuration defaults audit output to `~/.aihi/audit.jsonl`; a project file defaults to `<workspace>/.aihi/audit.jsonl`.

Minimal example:

```toml
[provider]
name = "deepseek"
models = ["deepseek-chat", "deepseek-reasoner"]
api_key_env = "DEEPSEEK_API_KEY"

# Every provider profile has its own model catalog. `model` is optional and
# defaults to the first entry in `models` (the old single-model form remains
# supported).
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

API keys stay in environment variables; configuration exposes only non-secret metadata through `config.get`. `permission_mode` is persisted with the run startup configuration. Hard safety denies remain active in every mode. Host execution is fail-closed and is not an isolation boundary; interactive acknowledgement is stored for the exact workspace/root in `~/.aihi/host-workspaces.json`.

Each provider can expose multiple models through `models = [...]`. The provider's active/default model is `model` or the first catalog entry. A model is valid only for the provider that declares it; `config.get` returns the non-secret provider/model catalog to clients.

## Skills and subagents

Built-in Skills (`code_review`, `debug`, `refactor`, and `test_writing`) are package content and are trusted implicitly. User, project, and workspace Skills require explicit trust before loading. The model-facing Skill index is emitted only when the load tool is available; use the `load_skill` tool with the plain Skill name (for example `code_review`), not a display name with a version suffix.

Named subagents are selected through the `task` tool: `explore`, `code_review`, `test`, and `general`. The default configuration limits subagents to depth 1, three children, and read-only filesystem capabilities. A Worker session store is required so parent and child runs can be replayed together.

## Audit and operational behavior

Each run emits redacted, bounded observations to `audit.jsonl` by default. Files are created with owner-only permissions (`0600`); `/doctor` reports file and parent-directory writability without creating a missing audit file. Disable the sink with `[audit] enabled = false` or set a path relative to the declaring TOML file.

Tool calls are persisted before execution and return exactly one result. Approval resolution is separate from resume: a client resolves an approval and then calls `run.resume`. This keeps the Worker protocol deterministic while allowing the TUI to present the two operations as one interaction.

## Development and tests

```bash
uv run pytest packages/aihi/code-agent/tests
uv run ruff check packages/aihi/code-agent
uv run mypy
uv run python -m build --wheel --no-isolation packages/aihi/code-agent
```

Run the complete workspace checks from the [repository README](../../../README.md). The Worker protocol contract is tested with [`@aihi/code-protocol`](../code-protocol/README.md).

## Security boundaries

- Treat model output, tool input, MCP responses, Skills, and subagent output as untrusted.
- Keep credentials in environment variables or an external secret manager, never in TOML or event content.
- Do not use `HostBackend` as a sandbox; choose an isolated backend when process isolation matters.
- Keep a finite turn limit, review `permission_mode`, and require explicit host acknowledgement before enabling unsafe local execution.

## Related documentation

- [Agent foundation](../agent/README.md)
- [Worker protocol](../code-protocol/README.md)
- [CLI/TUI](../../../apps/aihi-code-cli/README.md)
- [Repository architecture](../../../docs/ARCHITECTURE.md)
