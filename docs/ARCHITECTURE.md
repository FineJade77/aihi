# AIHI Architecture

[English] | [简体中文](ARCHITECTURE.zh-CN.md)

> Stable architecture and public-boundary guide for the AIHI monorepo.

| Field | Value |
| --- | --- |
| Status | Active baseline |
| Scope | aihi-models, aihi-agent, aihi-code-agent, @aihi/code-protocol, @aihi/code-cli |
| Runtime | Python 3.11+; TypeScript/Ink CLI |
| Protocol | Code Protocol 0.2 |
| Source of truth | Event log for runtime state; this document for stable boundaries |

This document describes contracts, ownership, dependency direction and safety invariants. Delivery status
belongs in [TASK.md](TASK.md). ADR and RFC files under docs/adr/ and docs/rfcs/ are local working notes
and are intentionally not published to Git.

## Contents

- [Product boundary](#product-boundary)
- [System topology](#system-topology)
- [Repository layout](#repository-layout)
- [Package responsibilities](#package-responsibilities)
- [Runtime and event model](#runtime-and-event-model)
- [Models and providers](#models-and-providers)
- [Context cache and compaction](#context-cache-and-compaction)
- [Tools and safety](#tools-and-safety)
- [Skills, MCP and extensions](#skills-mcp-and-extensions)
- [Coding worker and TUI protocol](#coding-worker-and-tui-protocol)
- [Persistence and observability](#persistence-and-observability)
- [Extension rules](#extension-rules)
- [Quality gates](#quality-gates)

## Product boundary

AIHI is a reusable Agent Harness. A model produces intent; the Harness turns that intent into a durable,
governed and recoverable run. Product applications choose prompts, project rules, provider/model
profiles, product tools and user experience.

~~~text
aihi-models -> aihi-agent -> application runtime -> user interface
                             (aihi-code-agent)     (@aihi/code-cli)
~~~

The first product is Coding Agent. Other products such as Cowork should reuse the Harness instead of
copying its runtime.

### In scope

- Provider-neutral model messages, capabilities, streaming and adapters.
- Recoverable sessions, event-sourced runs, replay, branching and auditability.
- Context compilation, token budgets, compaction and artifact retention.
- Tool contracts, policy, approval, sandbox, hooks, skills, MCP, plugins and subagents.
- A local Coding Worker and a TypeScript TUI consuming the Worker protocol.

### Out of scope for base packages

- ModelRouter, ModelGateway, model roles and cross-provider fallback.
- Product prompts, project conventions, default model selection and product tool bundles.
- TUI, Web/Desktop UI and chat-channel integrations.
- Any claim that HostBackend is a security sandbox.

Those choices belong to an application or a future adapter. Base packages remain provider-neutral and
application-independent.

## System topology

~~~mermaid
flowchart TB
    UI["@aihi/code-cli\nTypeScript Ink TUI"]
    WORKER["aihi-code-agent\nWorker + Coding runtime"]
    PROTOCOL["@aihi/code-protocol\nJSON-RPC 2.0 / schemas"]
    AGENT["aihi-agent\nRuntime, sessions, tools, policy, sandbox"]
    MODELS["aihi-models\nmodel contracts + provider adapters"]
    STORE["SQLite EventStore\nartifacts + audit.jsonl"]
    PROVIDERS["Configured providers\nOpenAI / Anthropic / compatible / DeepSeek"]

    UI <-->|"stdio\nContent-Length"| PROTOCOL
    PROTOCOL <--> WORKER
    WORKER --> AGENT
    AGENT --> MODELS
    MODELS --> PROVIDERS
    AGENT --> STORE
    WORKER --> STORE
~~~

Every side effect follows:

~~~text
Tool input -> validation/preparation -> policy/approval -> hooks -> governed Tool execution -> durable Tool Result
~~~

The event log, not the model response or TUI memory, is the runtime source of truth.

## Repository layout

~~~text
packages/aihi/
├── models/                 # distribution: aihi-models; import: aihi.models
├── agent/                  # distribution: aihi-agent; import: aihi.agent
├── code-agent/             # distribution: aihi-code-agent; Coding Worker/runtime
└── code-protocol/          # npm package: @aihi/code-protocol; DTOs and JSON Schemas
apps/
└── aihi-code-cli/          # private npm app: @aihi/code-cli; Ink TUI
tests/
├── contract/               # cross-package public/schema contracts
├── integration/            # installed-wheel and runtime integration tests
├── packaging/              # wheel layout, namespace and typed-package checks
└── fixtures/               # frozen event, SQLite and trace compatibility data
docs/
├── ARCHITECTURE.md         # this stable boundary document
└── TASK.md                 # delivery roadmap and acceptance gates
~~~

Python packages use a PEP 420 aihi namespace. The namespace root has no __init__.py; each leaf package
owns its __init__.py, __all__ and py.typed marker.

## Package responsibilities

| Package | Owns | Must not own |
| --- | --- | --- |
| aihi-models | Message, blocks, ModelRequest/Response, Usage, capabilities, ModelToolDefinition, provider protocol, adapters and codec | Agent events, ToolSpec, policy, sandbox, router/gateway, model selection or credentials |
| aihi-agent | Runtime loop, sessions/event store, context/compaction, tools.ToolSpec, dispatcher, policy, approvals, sandbox, artifacts, skills, memory, MCP, plugins, subagents, eval and observability | Product prompts, provider profiles, TUI and application defaults |
| aihi-code-agent | Coding configuration, provider/model catalog, Worker process, RPC handlers, Coding tools and composition | A second Agent Runtime, provider adapters or a UI |
| @aihi/code-protocol | Versioned RPC methods, DTOs, event guards and JSON Schemas | Runtime state, persistence or tool execution |
| @aihi/code-cli | Ink presentation, slash commands, picker UX, transcript projection, input history and process lifecycle | Event-store writes, policy decisions, model calls or business truth |

Applications use each package's top-level public API and must not import private modules across boundaries.

## Runtime and event model

One user request creates one Run; a Session contains one or more runs:

~~~text
CREATED -> RUNNING -> WAITING_TOOL -> RUNNING
                         |
                         v
                   WAITING_APPROVAL -> WAITING_TOOL

RUNNING -> COMPLETED | FAILED | INTERRUPTED | CANCELLED
~~~

WAITING_APPROVAL is resumable, not terminal. INTERRUPTED can be resumed; CANCELLED is explicit
abandonment and cannot be resumed.

### Durable invariants

1. Persist the assistant tool call before executing it.
2. Every executed tool call receives exactly one durable result; a pending approval remains pending.
3. Append policy and tool outcomes immediately; streaming chunks are UI-only ephemeral events.
4. Resume uses the first run.started configuration: provider, model, application authority profile,
   prompt summary and output budget cannot drift. For the Coding Agent that profile freezes the Session
   workspace, AccessMode, RunMode and command-sandbox descriptor.
5. Cancellation and restart repair orphaned calls without blindly replaying unknown side effects.
6. A session has one writer and monotonic seq; appends use expected_seq for conflict detection.

An event envelope contains event_id, session_id, run_id, seq, type, schema_version, created_at and data.
Additive fields are compatible; removing or changing meaning requires a migration. Unknown envelope
versions fail closed.

## Models and providers

aihi.models.Provider is the only model boundary required by the Agent Runtime:

~~~python
capabilities(model)
stream(ModelRequest)
count_tokens(ModelRequest)
~~~

Adapters normalize provider differences into the same message and stream contracts. Current adapters
include Fake, OpenAI, Anthropic, OpenAI-compatible and DeepSeek. DeepSeek reuses the OpenAI-compatible
implementation with an explicit endpoint.

Multiple providers and models are an application concern. aihi-code-agent loads a provider catalog,
validates the selected model, and exposes the selection to the CLI. The CLI may switch profiles but
never implements routing or fallback.

Provider errors include a stable code and retryable flag. After the first stream chunk an attempt must
not silently retry or switch provider. Providers never execute tools. Credentials, endpoints, timeouts,
defaults and any future Gateway decorator are injected by the application.

## Context cache and compaction

The application-owned base system block and canonical model-visible Tool definitions form the stable
prompt-cache prefix. Dynamic sections, `ContextState`, Tool Result placeholders and current turns stay
after that boundary, so compaction never changes the stable cache family.

The Runtime measures the complete normalized request and uses 70%/85% soft/hard watermarks with a 60%
target. Soft pruning removes only durable, integrity-checked, Artifact-backed read-only Tool Result
bodies. Hard compaction projects evidence-backed schema-v2 `ContextState` from immutable Events, Tool
metadata and Artifact manifests, merges older state field by field, and retains a token-bounded raw tail
of complete Tool groups. Model enrichment cannot create file or verification receipts. Version-2
`compaction.created` records are additive; version-1 records and frozen stores remain replayable.

Cache observability is durable but contains no prompt or cache key: each `model.usage` event records
Provider-reported cache read/write tokens, a SHA-256 of the cache-family key, full-request pressure and
any pruning decision. Evaluation aggregates cache-hit ratio, cache-key changes and soft/hard
compaction counts. The replay-only golden Trace and the application-owned
`aihi-code-agent-context-v1` comparison require unchanged cache-family identity, 100% critical-state
recall and task success before accepting a token reduction; wall-clock latency is diagnostic only.

## Tools and safety

aihi.models.ModelToolDefinition contains only model-visible name, description and JSON Schema.
aihi.agent.tools.ToolSpec adds mutation, concurrency, idempotency, capability, timeout and approval
governance.

| Class | Examples | Default behavior |
| --- | --- | --- |
| Read-only | read_file, glob, grep | Allowed in every AccessMode and RunMode; may run concurrently when safe |
| Workspace mutation | write_file, edit_file | Denied by read_only/plan; allowed by workspace_write/full_access |
| Process execution | bash | Denied by read_only/plan; ASK in workspace_write; ALLOW in full_access |

Policy returns ALLOW, DENY or ASK. ASK persists approval.requested and suspends the run; the application
supplies the human resolver. Approval and capability leases are append-only, scoped to run_id and
reconstructed on resume. One-shot approvals are consumed exactly once.

The Coding workspace is the canonical cwd stored in its Session; TOML may discover configuration from
that directory but cannot define another workspace. File tools canonicalize and operate locally through
the application context. Only Bash owns a Sandbox backend. HostBackend requires explicit unsafe=true and
provides command cwd, timeouts, output limits and process-group cleanup, not isolation. Docker command
execution fails closed when required capabilities are unavailable.

The base Harness governs child budget and capability subsets, depth and child count; it treats cwd and
application authority as opaque values. Code Agent keeps a child in the parent's canonical Session cwd
and injects a child `CodeAgentPermissionContext`: granted capabilities determine the requested
AccessMode, the parent AccessMode remains the ceiling, and Plan forces a read-only Plan child.

## Skills, MCP and extensions

Optional capabilities enter through RuntimeExtensions, not hard-coded imports in the context compiler:

- Skills are discovered as metadata and hashes; only an explicit request loads the body.
- Built-in skills are implicitly trusted by package integrity. User/project/workspace skills require
  exact trust for name, version, scope and content hash.
- MCP and plugin tools register through ToolRegistry and share policy and hook governance; a tool that
  executes arbitrary commands must explicitly own an application-supplied command Sandbox.
- Plugins are discovered without execution and activated in a separate bounded host process.
- Memory contributes scoped, sanitized context and requires explicit access for writes.
- Subagents run as ordinary governed task tools in independent sessions.

## Coding worker and TUI protocol

aihi-code-agent is the application runtime and sole EventStore writer. @aihi/code-cli is a thin local
client. They communicate over stdio using Code Protocol 0.2:

- JSON-RPC 2.0 with Content-Length framing and an exact-version initialize handshake.
- run.start and run.resume immediately return acceptance containing run_id.
- Progress and terminal states arrive as versioned notifications; startup failures use run.error.
- Reconnects replay session.events(after_seq) before accepting live notifications.
- The TUI uses one reducer for replay and live events, de-duplicates by seq, and treats canonical
  assistant.message as authoritative over temporary model.chunk buffers.

The TUI owns presentation state only: viewport, collapsed tool output, slash completion and draft
history are not persisted. User messages and runtime events are persisted by the Worker.

## Persistence and observability

SQLite WAL is the default local store. Artifacts hold large outputs, diffs and attachments outside the
prompt with session/run retention and explicit access checks. Snapshots and compaction are derived
accelerators; they never replace original events. Event envelope v2 removes the legacy Harness-owned
workspace from `subagent.spawned` task payloads; the registered v1 migration keeps frozen sessions
readable without preserving that application-specific field in current task types.

audit.jsonl is a local, redacted operational log and never runtime truth. /doctor checks its configured
target and recent writable parent; failures are surfaced without changing the run result. Trace export,
replay and eval operate on redacted event bundles and never re-execute tools or providers.

## Extension rules

1. Decide whether a capability is provider-neutral and reusable across products.
2. Define protocol, event types and failure semantics first.
3. Put product defaults and UX in the application layer.
4. Keep all side effects on the tool -> policy -> hooks -> governed execution path; inject a command
   Sandbox only into tools that execute arbitrary commands.
5. Add compatibility, security and installed-package tests before exposing a public symbol.

If an implementation must change RunCoordinator semantics, relax a security default or introduce a
product-specific prompt/role, stop and review the boundary before coding.

## Quality gates

~~~bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
pnpm --dir apps/aihi-code-cli test
~~~

Packaging tests build and install wheels independently and together, verify PEP 420 namespace layout and
py.typed, and replay the frozen event/SQLite/trace fixtures without regenerating them. Contract tests read
the source of all three distributions and fail the build when a package imports a layer above it, reaches
into another distribution's private or internal module instead of its package surface, or exports an
`__all__` name nothing binds. Wheel metadata cannot catch those: in a development checkout every `src`
tree is importable, so a reversed import type checks and runs. The three Python
distributions are published as `0.1.0` on PyPI: [aihi-models](https://pypi.org/project/aihi-models/0.1.0/),
[aihi-agent](https://pypi.org/project/aihi-agent/0.1.0/) and
[aihi-code-agent](https://pypi.org/project/aihi-code-agent/0.1.0/).

See [TASK.md](TASK.md) for the delivery matrix and each package README for API usage.
