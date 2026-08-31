# AIHI

[English] | [简体中文](README.zh-CN.md)

AIHI is a small, provider-neutral foundation for building recoverable AI agents, plus a coding-agent runtime and a TypeScript terminal UI.

The repository is a Python/TypeScript monorepo. The Python packages own model contracts and agent execution; the TypeScript packages own the wire contract and the local TUI.

## Highlights

- Provider-neutral model contracts with OpenAI, Anthropic, DeepSeek, OpenAI-compatible, and fake providers.
- Application-layer configuration supports multiple provider profiles and a model catalog per provider.
- Durable, event-sourced agent sessions with resumable runs, compaction, approvals, and bounded turn budgets.
- Policy-aware coding tools, sandbox backends, Skills, MCP, subagents, artifacts, and redacted audit/telemetry logs.
- A language-neutral JSON-RPC protocol shared by the Python Worker and TypeScript CLI.
- A local Ink TUI with session recovery, model/provider selection, approvals, Skills/MCP inspection, and diagnostics.
- A versioned Agent evaluation corpus and full CI gates; the first reviewed live DeepSeek baseline passed 26/27 attempts (96.3% empirical pass@1) across nine tasks repeated three times.

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
docs                        Architecture and project task notes (ADR/RFC drafts are local-only)
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

The public PyPI surface contains only the two foundation distributions:

```bash
python -m pip install aihi-models==0.2.0 aihi-agent==0.2.0
```

`aihi-code-agent` is a private local application and is not published to PyPI. It is intentionally
outside the uv workspace; install it separately when running the Worker or CLI from source:

```bash
uv pip install -e packages/aihi/code-agent
```

### Run checks

```bash
uv run python -m compileall -q packages
uv run pytest
uv run ruff check .
uv run mypy
pnpm --dir packages/aihi/code-protocol typecheck
pnpm --filter @aihi/code-cli typecheck
pnpm --filter @aihi/code-cli test
```

`.github/workflows/ci.yml` runs these quality gates on Python 3.11/3.12 and
Node.js 20, including the installed-wheel packaging tests. Deterministic and
live Agent evaluation gates are documented in
[`docs/EVALUATION.md`](docs/EVALUATION.md); live mode supports repeated,
multi-model comparisons with pass@1, latency, token/tool usage and cost metrics.
The credential-free reviewed baseline is available in
[`evals/aihi_code_agent/v1/baselines/`](evals/aihi_code_agent/v1/baselines/).

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
| `aihi-models` | Provider-neutral messages, streaming chunks, errors, serialization, and provider adapters | [README](packages/aihi/models/README.md) · [PyPI](https://pypi.org/project/aihi-models/0.2.0/) |
| `aihi-agent` | Event-sourced runtime, tools, policy, sandbox, sessions, context, Skills/MCP, and observability | [README](packages/aihi/agent/README.md) · [PyPI](https://pypi.org/project/aihi-agent/0.2.0/) |
| `aihi-code-agent` | Private local application outside the uv workspace: Coding prompts, configuration, Worker RPC, tools, Skills, MCP, and subagents | [README](packages/aihi/code-agent/README.md) |
| `@aihi/code-protocol` | Shared TypeScript DTOs and JSON Schemas for the Worker boundary | [README](packages/aihi/code-protocol/README.md) |
| `@aihi/code-cli` | Local Ink TUI for the coding Worker | [README](apps/aihi-code-cli/README.md) |

## Design principles

### Recoverability first

The session event log is the source of truth. A run can be interrupted, resumed, or inspected without reconstructing state from transient UI output. Context compaction creates derived summaries; it never rewrites history.

### Explicit side effects

Tool calls are persisted before execution and produce exactly one result. The execution path is validation/preparation → policy → approval → hooks → governed tool execution. Only tools that execute arbitrary commands receive a Sandbox backend. An `ASK` decision suspends a run and is recoverable through the protocol.

### Safe defaults

The host sandbox is not an isolation boundary and requires an explicit unsafe acknowledgement. External Skills require trust; built-in Skills are trusted as package content. Audit and telemetry sinks redact sensitive values and use owner-only files by default.

### Small foundation packages

`aihi-models` does not contain a router or gateway. `aihi-agent` does not choose providers or hide tool defaults. Application-specific composition belongs in `aihi-code-agent` or another application layer.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Project tasks](docs/TASK.md)
- ADR/RFC drafts are kept locally in `docs/adr/` and `docs/rfcs/` and are intentionally excluded from Git.

## Status

AIHI is an actively developed workspace. The public `aihi-models` and `aihi-agent` distributions are
available on PyPI at `0.2.0`; `aihi-code-agent` remains a private local application outside the uv
workspace. Public API stability and
packaging guarantees continue to evolve toward a 1.0 release.

## License

MIT
