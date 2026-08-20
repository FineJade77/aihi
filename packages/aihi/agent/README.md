# aihi-agent

[English] | [简体中文](README.zh-CN.md)

Provider-neutral, recoverable agent runtime for AIHI.

`aihi-agent` turns model contracts into a durable execution system. It provides the loop, sessions, tools, policy, approvals, sandbox boundary, context management, integrations, and observability that an application can compose for a specific product.

## Responsibilities

- Run bounded model/tool turns with explicit runtime composition.
- Persist an append-only event log and recover sessions after interruption.
- Compile stable-prefix-aware context and compact it into derived state without rewriting history.
- Register and execute tools through policy, approvals, hooks, and a sandbox backend.
- Integrate Skills, MCP servers, subagents, memory, artifacts, telemetry, replay, and evaluations.

The package does **not** select a provider, implement a UI, provide a model router/gateway, or hide tool defaults. Applications pass those choices to `RuntimeBuilder`.

## Architecture

```text
Model Provider (aihi-models)
              │
              ▼
RuntimeBuilder ──► Runtime / RunCoordinator ──► EventStore
              │                  │
              │                  ├── ContextCompiler / Compaction
              │                  ├── ToolRegistry ──► Policy ──► Approval
              │                  │                         │
              │                  │                         ▼
              │                  └── Hooks ──► SandboxBackend ──► Tool
              │
              └── Skills / MCP / Subagents / Memory / Artifacts / Telemetry
```

The event store is the source of truth. Tool calls are recorded before execution and have exactly one result. An approval decision of `ASK` suspends the run so it can be resumed later.

## Installation

Published release:

```bash
python -m pip install aihi-agent==0.1.0
```

See the [PyPI project page](https://pypi.org/project/aihi-agent/0.1.0/). It installs the compatible
`aihi-models` dependency automatically. For repository development:

From the workspace:

```bash
uv sync
```

For a local editable install:

```bash
uv pip install -e packages/aihi/agent
```

`aihi-agent` requires Python 3.11+ and depends on `aihi-models` 0.1.x.

## Minimal runtime

```python
from pathlib import Path

from aihi.agent import HostBackend, InMemoryEventStore, ReadFileTool, RuntimeBuilder, Session
from aihi.models import FakeProvider, FakeStep, Message

provider = FakeProvider([FakeStep(text="I inspected the workspace.")])
runtime = (
    RuntimeBuilder(
        provider=provider,
        model="fake-model",
        sandbox=HostBackend(Path.cwd(), unsafe=True),
        tools=[ReadFileTool()],
    )
    .with_max_turns(20)
    .build()
)

session = Session.create(
    InMemoryEventStore(),
    cwd=Path.cwd(),
    provider="fake",
    model="fake-model",
)

result = await runtime.coordinator.run(
    session,
    model=runtime.model,
    user_message=Message.text("user", "Inspect this project."),
)
print(result.state)
```

For real applications, prefer an isolated backend when available. `HostBackend` is a controlled local execution backend, not a security isolation boundary, and requires an explicit `unsafe=True` acknowledgement.

## Runtime composition

`RuntimeBuilder` requires the important dependencies up front:

- `provider` and `model`;
- a `sandbox` backend;
- the application-approved `tools` collection.

Optional extensions are added explicitly with methods such as:

- `.with_max_turns(...)` and `.with_context_window(...)`;
- `.with_policy(...)`, `.with_approvals(...)`, and `.with_hooks(...)`;
- `.with_skills(...)`, `.with_memory(...)`, `.with_compaction(...)`;
- `.with_subagents(...)`, `.with_artifacts(...)`, and `.with_telemetry(...)`.

The default coordinator turn budget is finite (`100`) and can be lowered for a product-specific safety envelope.

## Core modules

| Area | Main API |
| --- | --- |
| Runtime and runs | `Runtime`, `RuntimeBuilder`, `RunCoordinator`, `RunResult`, `RunState` |
| Sessions and storage | `Session`, `EventStore`, `InMemoryEventStore`, `SQLiteEventStore`, `Event` |
| Context | `ContextCompiler`, `CompactionPolicy`, `ContextState`, summaries and compaction generators |
| Tools | `Tool`, `ToolSpec`, `ToolContext`, `ToolRegistry`, built-in file/shell tools |
| Policy and approval | `PermissionMode`, `DefaultPolicyEngine`, `Approval`, approval resolvers |
| Sandbox | `HostBackend`, `LocalIsolatedBackend`, `DockerBackend` |
| Integrations | Skills, MCP, plugins, subagents, memory, artifacts |
| Observability | `Telemetry`, `JsonlTelemetrySink`, `InMemoryTelemetrySink` |
| Verification | replay, golden tasks, evals, and contract helpers |

## Tool and approval model

Tools are registered with explicit `ToolSpec` metadata. The policy engine decides whether an invocation is allowed, denied, or must ask for approval. Approval leases can scope a decision to a request, a tool, or a run according to the application policy.

Use the built-in tools only with a sandbox and policy appropriate for the workspace. File reads, glob/grep, edits, writes, and shell execution should not be treated as interchangeable capabilities.

## Observability

Telemetry is an observation stream, not the event log. `JsonlTelemetrySink` emits redacted, bounded records and creates owner-only files by default. Use the event store for recovery and audit the telemetry stream for operational diagnosis; do not use UI output as a source of truth.

## Stable context prefix

`ContextCompiler` keeps the application-owned base system prompt in a stable `TextBlock` and places
runtime `ContextSection` values in the dynamic suffix. `RunCoordinator` derives one cache-family key
from that stable prefix and canonical model-visible tool definitions. Dynamic memory, skills,
compaction state and current turns remain after the cache boundary. Cache availability never changes
Event replay, policy, approval, sandbox or tool persistence semantics.

## Development

```bash
uv run pytest packages/aihi/agent/tests
uv run ruff check packages/aihi/agent
uv run mypy
uv run python -m build --wheel --no-isolation packages/aihi/agent
```

See the repository [architecture guide](../../../docs/ARCHITECTURE.md) and the [code-agent README](../code-agent/README.md) for an application-level composition.

## Security model

- Keep credentials in the application/provider boundary, never in prompts or event payloads.
- Treat model output, tool arguments, Skills, MCP responses, and subagent output as untrusted.
- Do not claim that `HostBackend` isolates a process; use `LocalIsolatedBackend` or `DockerBackend` when isolation is required.
- Set a finite turn limit and review approval/policy defaults before exposing tools to a model.
