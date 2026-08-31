# Contributing to AIHI

AIHI is a Python/TypeScript monorepo for building recoverable, provider-neutral Agent runtimes and
applications. This guide is the contributor and coding-agent contract: it explains where changes belong,
which invariants must never regress, how to validate a change and what to include in a review.

> Before changing code, read [ARCHITECTURE.md](docs/ARCHITECTURE.md) and [TASK.md](docs/TASK.md).
> The Chinese versions are [ARCHITECTURE.zh-CN.md](docs/ARCHITECTURE.zh-CN.md) and
> [TASK.zh-CN.md](docs/TASK.zh-CN.md). ADR/RFC files under docs/adr/ and docs/rfcs/ are local-only
> working notes and are intentionally ignored by Git.

## Contents

- [Project map](#project-map)
- [Choose the right layer](#choose-the-right-layer)
- [Development setup](#development-setup)
- [Validation commands](#validation-commands)
- [Change workflow](#change-workflow)
- [Runtime invariants](#runtime-invariants)
- [Security and sandbox rules](#security-and-sandbox-rules)
- [Package-specific rules](#package-specific-rules)
- [Compatibility and persistence](#compatibility-and-persistence)
- [Documentation and review checklist](#documentation-and-review-checklist)

## Project map

```text
packages/aihi/models/        aihi-models: model contracts and Provider adapters
packages/aihi/agent/         aihi-agent: recoverable Agent Runtime
packages/aihi/code-agent/    aihi-code-agent: Coding application runtime and Worker
packages/aihi/code-protocol/ @aihi/code-protocol: RPC DTOs and JSON Schemas
apps/aihi-code-cli/          @aihi/code-cli: private TypeScript/Ink TUI
tests/                       cross-package contract, integration, packaging and fixtures
docs/                         stable architecture and roadmap; ADR/RFC drafts remain local
```

Dependency direction is one-way:

```text
aihi-models -> aihi-agent -> application runtime -> UI
                           aihi-code-agent     @aihi/code-cli
```

aihi.models must not import aihi.agent; base packages must not import applications; applications must
not copy runtime or safety implementations. aihi.agent.agents is subagent coordination infrastructure,
not the directory for a user-facing Agent product.

The three Python distributions are published at version 0.1.0:

- [aihi-models on PyPI](https://pypi.org/project/aihi-models/0.1.0/)
- [aihi-agent on PyPI](https://pypi.org/project/aihi-agent/0.1.0/)
- [aihi-code-agent on PyPI](https://pypi.org/project/aihi-code-agent/0.1.0/)

## Choose the right layer

| Change | Put it in | Do not put it in |
| --- | --- | --- |
| Message, model request/response, stream chunk, Provider error or adapter | packages/aihi/models | aihi-agent or an application |
| Runtime, Session, EventStore, Context, ToolSpec, Policy, Sandbox, Skill, MCP, Memory, Subagent, Eval or Observability contract | packages/aihi/agent | Coding prompts, UI or product defaults |
| Coding prompt, workspace rules, Provider/Model catalog, access/run-mode defaults, Coding tools or Worker composition | packages/aihi/code-agent | The two base packages |
| RPC method, DTO, event guard or JSON Schema shared by Worker and clients | packages/aihi/code-protocol | Runtime state or persistence |
| Slash command, picker, transcript viewport, composer or terminal presentation | apps/aihi-code-cli | Worker business truth or EventStore writes |
| Product-specific behavior | The relevant application | A reusable base package |

When a gap may be reusable across products, record an H-* item in [TASK.md](docs/TASK.md) before
moving it into a base package. Product-specific prompts, roles, tool bundles, credentials and UX stay
in the application layer. Base packages expose only their top-level public APIs; do not import private
modules across package boundaries.

## Development setup

Requirements:

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20+
- pnpm 9

Install the workspace:

```bash
uv sync
pnpm install
```

For normal use, install the published Coding Agent:

```bash
python -m pip install aihi-code-agent==0.1.0
```

For repository development, use editable installs only when you need to test source changes:

```bash
uv pip install -e packages/aihi/models
uv pip install -e packages/aihi/agent
uv pip install -e packages/aihi/code-agent
```

The root pyproject.toml configures test paths, namespace source paths, Ruff and strict mypy.
Do not add dependencies to a package merely to simplify a local test; update the package manifest and
the packaging/integration tests together.

## Validation commands

Run the smallest relevant check first, then the full suite before handoff.

### Python

```bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
```

Useful focused commands:

```bash
python3 -m pytest packages/aihi/models/tests
python3 -m pytest packages/aihi/agent/tests
python3 -m pytest packages/aihi/code-agent/tests
python3 -m pytest packages/aihi/agent/tests/security
```

### TypeScript and CLI

```bash
pnpm --dir apps/aihi-code-cli typecheck
pnpm --dir apps/aihi-code-cli test
pnpm --dir packages/aihi/code-protocol typecheck
```

### Packaging

Build all Python distributions:

```bash
mkdir -p dist/pypi
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/models
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/agent
python3 -m build --sdist --wheel --no-isolation --outdir dist/pypi packages/aihi/code-agent
python3 -m twine check dist/pypi/*
```

Packaging tests must verify wheel layout, PEP 420 namespace coexistence, py.typed, dependency metadata,
installed-wheel smoke behavior and frozen compatibility fixtures. Never regenerate a frozen fixture merely
to make a changed implementation pass.

## Change workflow

1. **Scope the change.** Identify the owning package, public API and current H-*/P-* task.
2. **Read the contract.** Check the relevant architecture section, package README and existing tests.
3. **Write the test first.** Add a unit, contract, security, integration, packaging or UI regression test.
4. **Implement through injection points.** Keep product choices in applications and side effects in the governed tool path.
5. **Run focused checks.** Include an installed-wheel check when distribution metadata or package layout changes.
6. **Update documentation.** Keep English and Chinese README/architecture/task documents in sync.
7. **Review the diff.** Remove generated files, credentials, local logs and unrelated formatting changes.
8. **Commit one reviewable slice.** Keep unrelated cleanup out of the same commit and report the validation run.

### Public API changes

aihi.models.__all__ and aihi.agent.__all__ are the supported cross-distribution composition surfaces.
If a public symbol changes, update its contract tests and package README. If event schema, protocol or
security defaults change, update the stable architecture/task docs and record the decision in local
ADR/RFC notes for review.

### Dependency changes

Keep the dependency graph acyclic. A new runtime dependency must be justified in the package manifest,
lock/workspace metadata, packaging tests and the relevant README. Optional integrations should be
injected through Protocols rather than imported by the core loop.

## Runtime invariants

These rules are not implementation preferences; tests and code review must preserve them.

- Persist an Assistant Tool Call before executing the tool.
- Every executed Tool Call has exactly one durable Tool Result. A pending approval remains unpaired only
  until Resume executes it or a matching denial records its result.
- A Policy result of ASK appends approval.requested and suspends the Run in WAITING_APPROVAL; never fabricate
  a result to continue.
- The event log is the source of truth. Ephemeral model chunks are for observers/UI and do not replace
  durable messages or results.
- Resume reuses the first run.started Provider, Model, opaque application authority profile, prompt
  summary and output budget; it cannot weaken or drift authority.
- INTERRUPTED is resumable; CANCELLED is explicit abandonment and is not resumable.
- A Session has one writer and monotonic seq; appends use expected_seq for conflict detection.
- Provider fallback must never blindly replay a possibly side-effecting Tool.
- After the first Provider stream chunk, do not automatically retry or switch Provider.
- Events, errors, messages and tool results must be JSON serializable and reloadable.
- Child Agents run in independent Sessions with stricter subsets of parent capabilities, budget and
  application-owned authority.

## Security and sandbox rules

### Tool execution

All side effects follow:

```text
Tool input -> validation/preparation -> policy/approval -> hooks -> governed Tool execution -> durable Tool Result
```

- ToolSpec owns execution governance; ModelToolDefinition contains only model-visible fields.
- Validate and normalize tool input before Policy evaluation.
- Read-only and concurrency-safe tools may run in parallel; mutating or non-safe tools run serially,
  with results committed in call order.
- The default reusable Policy asks before process execution; application Policy owns any stricter or
  explicitly broader mode matrix.
- Sensitive-path checks in command text are heuristics, not a security boundary.
- Hooks and remote MCP/Plugin tools cannot bypass Policy or Approval. A tool executing arbitrary
  commands must own an explicitly injected command Sandbox.

### Host and isolation

HostBackend is a controlled local backend, not a security isolation boundary:

- Construction and execution require explicit unsafe=true.
- A command tool records its non-secret Sandbox descriptor in `PreparedToolCall.execution`; generic
  Run and Tool events do not claim a global Sandbox.
- Enforce command-root canonicalization, timeouts, output limits and process-group cleanup. Application
  file tools separately own workspace canonicalization and symlink escape checks.
- Do not claim Host provides filesystem or network isolation.
- Local-isolated and Docker backends must fail closed when required capabilities are unavailable.
- Applications requiring isolation must reject Host during command-backend selection.

## Package-specific rules

### aihi-models

- Own model canonical types, Message codec, Provider Protocol, adapters and model-facing errors.
- Keep Agent Event, Policy, Sandbox and execution metadata out of the package.
- DeepSeek uses the OpenAI-compatible implementation with an explicit endpoint.
- Credentials and model catalogs are application-owned; adapters must not silently read environment variables.
- Provider constructors and contract tests must preserve timeout, retryability and first-chunk semantics.

### aihi-agent

- Keep RuntimeBuilder explicit: Provider, Model and Tools are application choices. Inject a Sandbox
  only into tools that execute arbitrary commands.
- Do not add a default_runtime() that silently selects Provider or tools.
- Keep optional abilities behind RuntimeExtensions, ContextContributor and RunRecorder.
- Use SQLite WAL by default; large outputs belong in Artifact Store, not prompt history.
- Context contributors fail closed; telemetry/recording failures remain observational and fail open.

### aihi-code-agent

- Own TOML config discovery, Coding prompts, Provider/Model catalogs, AccessMode/RunMode, Worker composition,
  Coding tools, Skill/MCP/subagent wiring and local audit.
- User config is ~/.aihi/aihi-code.toml; project config is <workspace>/.aihi/aihi-code.toml.
  Do not introduce a config-directory CLI override.
- Worker is the sole EventStore writer and communicates using Code Protocol 0.3.
- Built-in Skills are trusted by package integrity; other scopes require explicit trust and hash validation.
- Keep ModelRouter/ModelGateway and cross-provider fallback out of the base packages.

### @aihi/code-protocol and @aihi/code-cli

- Keep the protocol language-neutral, versioned and JSON serializable.
- run.start/run.resume return immediate acceptance; progress and terminal state arrive as notifications.
- Reconnect by replaying session.events(after_seq) before relying on live notifications.
- The CLI owns projection, viewport, composer and picker state; the Worker owns runtime truth and writes.
- Do not persist drafts, viewport state or arbitrary tool input in the EventStore.

## Compatibility and persistence

- New durable Event types must be registered in the Agent schema and covered by frozen compatibility data.
- Removing or changing the meaning of an existing Event field requires a schema version and migration.
- Message JSON changes require a versioned codec and Message -> EventStore -> Session reload -> Replay coverage.
- Old JSON, SQLite and Trace fixtures are compatibility contracts; do not rewrite them to fit new code.
- A Session fork creates a normal independent Session with a copied prefix; the parent remains immutable.
- Compaction, Memory, Snapshots, Trace and Eval are derived data and must not overwrite original Events.
- audit.jsonl is a redacted, best-effort operational log, never the runtime source of truth.
- Do not commit API keys, tokens, credentials, complete environment dumps, unredacted model/tool output,
  local .aihi state or generated build artifacts.

## Documentation and review checklist

Before opening or handing off a change, confirm:

- [ ] The owning package and dependency direction are correct.
- [ ] Public API, event/protocol schema and security impact are identified.
- [ ] Focused tests and the full relevant quality gates pass.
- [ ] Installed-wheel checks were run for packaging/layout changes.
- [ ] English and Chinese documentation are synchronized.
- [ ] No secrets, generated artifacts, local ADR/RFC files or unrelated edits are included.
- [ ] Commit message clearly describes one reviewable change.

For a new reusable capability, update [TASK.md](docs/TASK.md), [TASK.zh-CN.md](docs/TASK.zh-CN.md), the
relevant package README and tests. Keep stable contracts in the architecture documents; keep local
decision rationale in ignored ADR/RFC files.

## License and conduct

Follow the repository license and communicate respectfully in code review. Prefer evidence from tests,
contracts and reproducible commands over assumptions.
