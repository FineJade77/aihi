# AIHI

AIHI is a small, provider-neutral foundation for building recoverable AI agents, plus a coding-agent runtime and a TypeScript terminal UI.

The repository is a Python/TypeScript monorepo. The Python packages own model contracts and agent execution; the TypeScript packages own the wire contract and the local TUI.

## Highlights

- Provider-neutral model contracts with OpenAI, Anthropic, DeepSeek, OpenAI-compatible, and fake providers.
- Durable, event-sourced agent sessions with resumable runs, compaction, approvals, and bounded turn budgets.
- Policy-aware coding tools, sandbox backends, Skills, MCP, subagents, artifacts, and redacted audit/telemetry logs.
- A language-neutral JSON-RPC protocol shared by the Python Worker and TypeScript CLI.
- A local Ink TUI with session recovery, model/provider selection, approvals, Skills/MCP inspection, and diagnostics.

## Architecture

```text
┌──────────────────────────────┐
│ apps/aihi-code-cli            │  TypeScript Ink TUI
│ @aihi/code-cli               │
└──────────────┬───────────────┘
               │ Content-Length framed JSON-RPC 2.0
┌──────────────▼───────────────┐
│ packages/aihi/code-agent     │  Coding-agent runtime + Worker
│ aihi-code-agent              │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│ packages/aihi/agent          │  Provider-neutral agent loop
│ aihi-agent                   │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│ packages/aihi/models         │  Model contracts + providers
│ aihi-models                  │
└──────────────────────────────┘

packages/aihi/code-protocol (@aihi/code-protocol) is the shared DTO/schema
boundary between the Worker and the CLI; it is not a second runtime layer.
```

The dependency direction is intentionally one-way:

`aihi-models ← aihi-agent ← aihi-code-agent`

Model routing, provider selection, application configuration, and UI concerns stay above the foundation packages.

## Repository layout

```text
packages/aihi/models        Python model contracts and provider adapters
packages/aihi/agent         Python runtime, tools, policy, sessions, and integrations
packages/aihi/code-agent    Coding-agent composition and stdio Worker
packages/aihi/code-protocol TypeScript protocol types and JSON Schemas
apps/aihi-code-cli          Private TypeScript/Ink terminal application
tests                       Contract, integration, packaging, and fixture tests
docs                        Architecture, ADRs, and project task notes
```

## Quick start

### Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 or newer
- pnpm 9 (the version declared in `package.json`)

### Install the workspace

```bash
uv sync
pnpm install
```

If you are using the Python Worker outside the workspace environment, install the coding-agent package in editable mode:

```bash
uv pip install -e packages/aihi/code-agent
```

### Run checks

```bash
uv run pytest
uv run ruff check .
uv run mypy
pnpm --filter @aihi/code-cli typecheck
pnpm --filter @aihi/code-cli test
```

### Run the coding CLI

Build the TUI and start it against a project directory:

```bash
pnpm --filter @aihi/code-cli build
node apps/aihi-code-cli/dist/main.js --workspace /path/to/project
```

The CLI launches `aihi-code-agent` as a local stdio Worker. See the [CLI README](apps/aihi-code-cli/README.md) for flags, slash commands, configuration, and recovery workflows.

## Package guides

| Package | What it provides | Documentation |
| --- | --- | --- |
| `aihi-models` | Provider-neutral messages, streaming chunks, errors, serialization, and provider adapters | [README](packages/aihi/models/README.md) |
| `aihi-agent` | Event-sourced runtime, tools, policy, sandbox, sessions, context, Skills/MCP, and observability | [README](packages/aihi/agent/README.md) |
| `aihi-code-agent` | Coding-agent prompts, configuration, Worker RPC, coding tools, Skills, MCP, and subagents | [README](packages/aihi/code-agent/README.md) |
| `@aihi/code-protocol` | Shared TypeScript DTOs and JSON Schemas for the Worker boundary | [README](packages/aihi/code-protocol/README.md) |
| `@aihi/code-cli` | Local Ink TUI for the coding Worker | [README](apps/aihi-code-cli/README.md) |

## Design principles

### Recoverability first

The session event log is the source of truth. A run can be interrupted, resumed, or inspected without reconstructing state from transient UI output. Context compaction creates derived summaries; it never rewrites history.

### Explicit side effects

Tool calls are persisted before execution and produce exactly one result. The execution path is policy → approval → hook → sandbox → tool. An `ASK` decision suspends a run and is recoverable through the protocol.

### Safe defaults

The host sandbox is not an isolation boundary and requires an explicit unsafe acknowledgement. External Skills require trust; built-in Skills are trusted as package content. Audit and telemetry sinks redact sensitive values and use owner-only files by default.

### Small foundation packages

`aihi-models` does not contain a router or gateway. `aihi-agent` does not choose providers or hide tool defaults. Application-specific composition belongs in `aihi-code-agent` or another application layer.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Project tasks](docs/TASK.md)
- [Architecture decision records](docs/adr/)

## Status

AIHI is an actively developed workspace. The Python foundations and the local coding-agent path are usable; public API stability and packaging guarantees are still evolving toward a 1.0 release.

## License

License information will be added before the first public release.
