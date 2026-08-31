# aihi-agent

[English] | [简体中文](README.zh-CN.md)

Provider-neutral, recoverable agent runtime for AIHI.

`aihi-agent` turns model contracts into a durable execution system. It provides the loop, sessions, tools, policy, approvals, sandbox boundary, context management, integrations, and observability that an application can compose for a specific product.

## Responsibilities

- Run bounded model/tool turns with explicit runtime composition.
- Persist an append-only event log and recover sessions after interruption.
- Compile stable-prefix-aware context and compact it into derived state without rewriting history.
- Register and execute tools through validation, policy, approvals, and hooks.
- Execute model-authored commands through an explicitly selected command Sandbox backend.
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
              │                  ├── ToolRegistry ──► Policy ──► Approval ──► Hooks ──► Tool
              │                  │                                               │
              │                  │                                command Tool ──┘
              │                  │                                      │
              │                  │                                      ▼
              │                  │                         SandboxBackend.run_command
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
from aihi.agent import (
    InMemoryEventStore,
    RuntimeBuilder,
    Session,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)
from aihi.models import FakeProvider, FakeStep, Message


class InspectTool:
    spec = ToolSpec.define(
        name="inspect",
        description="Return application-owned inspection data.",
        input_schema={"type": "object", "properties": {}},
        concurrency_safe=True,
        mutates=False,
    )

    async def run(self, input, context: ToolContext) -> ToolExecutionResult:
        return ToolExecutionResult("Application inspection completed.")


provider = FakeProvider([FakeStep(text="I inspected the workspace.")])
runtime = (
    RuntimeBuilder(
        provider=provider,
        model="fake-model",
        tools=[InspectTool()],
    )
    .with_max_turns(20)
    .build()
)

session = Session.create(InMemoryEventStore(), metadata={"application": "example"})

result = await runtime.coordinator.run(
    session,
    model=runtime.model,
    user_message=Message.text("user", "Inspect this project."),
)
print(result.state)
```

Sandbox backends expose command execution only. They do not provide file read, glob, or write APIs.
For real command tools, prefer an isolated backend when available. `HostBackend` is a controlled
local execution backend, not a security isolation boundary, and requires an explicit `unsafe=True`
acknowledgement.

## Runtime composition

`RuntimeBuilder` requires the application-wide dependencies up front:

- `provider` and `model`;
- the application-approved `tools` collection.

A command tool may separately require a `SandboxBackend` in its own constructor. The Runtime does
not own or distribute a global Sandbox.

`Session.create(...)` accepts only application-owned JSON metadata plus generic identity and observer
options. The Harness persists and forks that metadata opaquely; it does not require or interpret a cwd,
provider, model, workspace, or product permission mode.

Optional extensions are added explicitly with methods such as:

- `.with_max_turns(...)` and `.with_context_window(...)`;
- `.with_policy(...)`, `.with_approvals(...)`, and `.with_hooks(...)`;
- `.with_skills(...)`, `.with_memory(...)`, `.with_compaction(...)`;
- `.with_subagents(...)`, `.with_artifacts(...)`, and `.with_telemetry(...)`.

Generic subagent governance covers capability and budget subsets, depth, and child count. The Harness
does not interpret a workspace or product permission mode. An application that enables subagents must
provide a child-context factory that derives its own opaque application authority and durable run
profile for every child Run, plus a Session factory that decides where and how child Sessions live.

The default coordinator turn budget is finite (`100`) and can be lowered for a product-specific safety envelope.

## Core modules

| Area | Main API |
| --- | --- |
| Runtime and runs | `Runtime`, `RuntimeBuilder`, `RunCoordinator`, `RunResult`, `RunState` |
| Sessions and storage | `Session`, `EventStore`, `InMemoryEventStore`, `SQLiteEventStore`, `Event` |
| Context | `ContextCompiler`, `CompactionPolicy`, `ContextState`, summaries and compaction generators |
| Tools | `Tool`, `ToolSpec`, `ToolContext`, `PreparedToolCall`, `ToolRegistry` |
| Policy and approval | `PermissionContext`, `DefaultPolicyEngine`, `Approval`, approval resolvers |
| Sandbox | `HostBackend`, `LocalIsolatedBackend`, `DockerBackend` |
| Integrations | Skills, MCP, plugins, subagents, memory, artifacts |
| Observability | `Telemetry`, `JsonlTelemetrySink`, `InMemoryTelemetrySink` |
| Verification | replay, golden tasks, evals, and contract helpers |

## Tool and approval model

Tools are registered with explicit `ToolSpec` metadata. The policy engine decides whether an invocation is allowed, denied, or must ask for approval. Approval leases can scope a decision to a request, a tool, or a run according to the application policy.

An application may pass typed, opaque state through `ToolContext.app_context` and
`PermissionContext.app_context`. A Tool can implement a deterministic, side-effect-free `prepare()`
method that returns `PreparedToolCall`; the normalized input is then shared by Policy and execution.
`RunCoordinator.run_profile` persists a JSON application authority snapshot and rejects a different
snapshot when the same Run is resumed.

The Harness ships no product file or shell tools. Applications own their tool set and local resource
semantics. A model-authored command tool should receive a command Sandbox explicitly at construction;
ordinary tools do not receive Sandbox access through `ToolContext`.

## Observability

Telemetry is an observation stream, not the event log. `JsonlTelemetrySink` emits redacted, bounded records and creates owner-only files by default. Use the event store for recovery and audit the telemetry stream for operational diagnosis; do not use UI output as a source of truth.

## Stable context prefix

`ContextCompiler` keeps the application-owned base system prompt in a stable `TextBlock` and places
runtime `ContextSection` values in the dynamic suffix. `RunCoordinator` derives one cache-family key
from that stable prefix and canonical model-visible tool definitions. Dynamic memory, skills,
compaction state and current turns remain after the cache boundary. Cache availability never changes
Event replay, policy, approval, command execution or tool persistence semantics.

`ContextPressureController` measures the complete normalized request against `ContextBudget.input_capacity`.
It uses the conservative local estimate by default, asks a capable Provider for an exact count at 65%,
and falls back without failing the run when counting is unavailable. `CompactionPolicy` applies the
60% target, 70% soft trigger and 85% hard trigger; a hard decision below 85% is allowed only when the
reserved next output would exhaust the following request. Each durable `model.usage` event records the
count method, current/projected pressure, trigger, reason and target. It also records Provider-reported
cache read/write tokens and a SHA-256 of the cache-family key, never the key or prompt itself.

At 70% pressure or above, the Runtime makes at most one batched soft-pruning attempt before the
Provider call. It removes only old, successful, read-only Tool Result bodies whose original Messages
are durable and whose session-scoped Artifacts pass access, manifest and payload-integrity checks.
Tool calls, result identity/error state, Artifact references, the recent complete-group tail and the
stable cache prefix remain unchanged. A batch is discarded unless it reclaims the configured minimum;
the immutable Event history is never rewritten.

At 85% pressure, or when reserved output predicts exhaustion, the Runtime replaces older complete
groups with a schema-v2 `ContextState` and keeps a token-bounded recent raw tail (20%, capped at 32K,
with at least four complete groups). Files, verification receipts, failures, pending approvals,
subagents and Artifacts are projected deterministically from immutable Events, Tool Result metadata
and Artifact manifests before optional model enrichment. Model output may add semantic constraints,
decisions, questions and next steps, but cannot assert file changes or successful verification. Each
`compaction.created` v2 event records evidence references, policy/count metadata and the retained tail;
v1 events remain replayable. Hard compaction must reach the 60% target or fail with
`context_window_exceeded`.

Applications can derive cache and compaction diagnostics entirely from durable Events:

```python
usage = [event for event in session.events if event.type == "model.usage"]
compactions = [event for event in session.events if event.type == "compaction.created"]
cached = sum(int(event.data["cached_input_tokens"]) for event in usage)
input_tokens = sum(int(event.data["input_tokens"]) for event in usage)
cache_hit_ratio = cached / input_tokens if input_tokens else 0.0
```

Run `python3 -m scripts.evals.run --mode pr` from a source checkout to execute the replay golden Trace,
the paired long-session cache/compaction gate and the Coding Agent smoke benchmark.

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
