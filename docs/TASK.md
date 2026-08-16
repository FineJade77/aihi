# AIHI Roadmap

[English] | [简体中文](TASK.zh-CN.md)

> Delivery plan for the AIHI monorepo. This is a living project board: every item has a status, scope
> and acceptance evidence.

| Field | Value |
| --- | --- |
| Status | Foundation complete; application and platform roadmap remains |
| Current release line | Python packages 0.1.0 on PyPI; Code Protocol 0.2 |
| Architecture | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Last completed slice | Multi-provider / multi-model catalog and selection UX |

Architecture decision records and RFC drafts in docs/adr/ and docs/rfcs/ are local-only working files.
They are deliberately ignored by Git; stable decisions must be reflected in architecture, package
READMEs, tests and public contracts.

## Contents

- [How to read this roadmap](#how-to-read-this-roadmap)
- [Delivered baseline](#delivered-baseline)
- [Current roadmap](#current-roadmap)
- [Definition of done](#definition-of-done)
- [Development workflow](#development-workflow)
- [Quality gates](#quality-gates)
- [Backlog rules](#backlog-rules)

## How to read this roadmap

| Status | Meaning |
| --- | --- |
| Done | Implemented, documented and covered by relevant tests |
| In progress | Current delivery slice; changes should stay narrowly scoped |
| Planned | Accepted direction with no implementation commitment yet |
| Deferred | Valid idea, waiting for a consumer or prerequisite |

M-* records the original foundation milestones, H-* records reusable Harness work and P-* records
application/platform work. New tasks must use one of these prefixes and name acceptance criteria before
implementation.

## Delivered baseline

The repository is a multi-package monorepo with a runnable local Coding Agent vertical slice.

| Area | Delivered capability | Status |
| --- | --- | --- |
| aihi-models | Provider-neutral messages, codecs, capabilities, token estimation and Fake, OpenAI, Anthropic, OpenAI-compatible and DeepSeek adapters | Done |
| aihi-agent | Recoverable loop, default turn budget, event store, replay, context/compaction, tools, policy, approvals, sandbox, artifacts, skills, MCP, plugins, memory, subagents, evals and audit hooks | Done |
| aihi-code-agent | Coding configuration, project/user .aihi config discovery, provider/model catalog, Worker, Session/Run/Task APIs, Coding tools and TUI composition | Done |
| @aihi/code-protocol | Code Protocol 0.2 DTOs, method map, guards and schemas | Done |
| @aihi/code-cli | Ink TUI, transcript replay, scrolling/composer UX, session/model pickers, slash commands, approvals, skills/MCP/tools management and doctor checks | Done |
| Packaging | Separate wheels, PEP 420 namespace, installed-wheel compatibility, frozen fixture replay and PyPI 0.1.0 publication | Done |
| Operability | Redacted local audit.jsonl, doctor audit target checks, session recovery and replay diagnostics | Done |

### Foundation history

The original M0–M7 foundation and H-01–H-17 Harness hardening are complete. They established the
public package boundary, event schema compatibility, safety invariants, context budgets, optional
capabilities and replay/eval surfaces. Historical milestone names remain useful for changelog and
fixture archaeology; new work should be filed under the roadmap below.

## Current roadmap

### P-01 — Coding CLI vertical slice

**Status: Done.** The local Worker/TUI path is usable end to end: configure a provider, select a model,
create or resume a session, stream a run, inspect durable events, handle approval, cancel/resume and
diagnose the local runtime.

| Slice | Scope | Acceptance |
| --- | --- | --- |
| P-01.1 | Code Protocol 0.2, non-blocking run acceptance, error and approval DTOs | Version handshake, runtime guards and protocol tests pass |
| P-01.2 | Event-driven transcript | Replay and live notifications use one reducer; sequence gaps trigger replay |
| P-01.3 | Transcript viewport and composer | Terminal-aware scroll/follow, folded tool output, multiline input and slash completion |
| P-01.4 | Session and model UX | Searchable session/provider/model pickers, /status, /doctor, cancel/resume |
| P-01.5 | Provider/model catalogs | Multiple provider profiles, multiple models per provider, /providers and /models, TUI catalog display and validation |
| P-01.6 | Local operability | Redacted audit.jsonl, doctor audit check, wheel isolation validation and regression tests |

### H-18 — Evaluation contract and Harness conformance

**Status: In progress.** The evaluation boundary is frozen in
[EVALUATION.md](EVALUATION.md). Harness cases belong to `evals/aihi_agent/` and
must exercise `aihi-agent` through redacted, replay-only traces.

| Slice | Scope | Acceptance |
| --- | --- | --- |
| H-18.1 | Versioned evaluation directories and JSON Schemas | `evals/schemas/` validates Harness cases and reports; English/Chinese contracts agree |
| H-18.2 | Harness conformance corpus and deterministic runner | Valid and rejected traces cover lifecycle, approval, recovery, authority and redaction; all required cases pass |

### P-06 — Coding Agent benchmark

**Status: In progress.** Product tasks belong to `evals/aihi_code_agent/` and
must grade actual isolated workspace outcomes. They may additionally export a
Harness Trace, but must not move Coding prompts, tools or product policy into
`aihi-agent`.

| Slice | Scope | Acceptance |
| --- | --- | --- |
| P-06.1 | Task, fixture, oracle and report contract | `code-task.schema.json` and `eval-report.schema.json` are versioned and documented |
| P-06.2 | Isolated task runner and deterministic graders | Hidden tests, regression, path scope, safety and trace results produce one machine-readable report |
| P-06.3 | Benchmark corpus and baseline | Initial task categories, fixed fixture hashes and pass@1 baseline are reproducible |
| P-06.4 | Offline/PR/nightly/release automation gates | `scripts/evals/run.py` has stable exit codes, writes redacted reports, PR uses the deterministic smoke baseline, and live modes require explicit Docker/no-network config |

### H-03–H-06 — platform adapters

**Status: Planned.** These capabilities are postponed until a real remote consumer exists. They may only
be added as adapters over existing protocols and must not change Runtime semantics.

| ID | Scope | Prerequisite |
| --- | --- | --- |
| H-03 | PostgreSQL EventStore | A concrete multi-user deployment requirement |
| H-04 | HTTP control plane, Worker lease and IPC authentication | A service boundary and threat model |
| H-05 | Production isolation profiles | Supported deployment targets and capability detection |
| H-06 | Remote telemetry/exporter | A governed sink, redaction policy and retention plan |

### P-02 — Cowork-ready Harness gaps

**Status: Planned.** Re-evaluate after a concrete Cowork workflow is specified. Promote a missing
capability into aihi-agent only when it is provider-neutral, reusable and contains no product prompts,
roles or UI policy.

### P-03 — Platform deployment

**Status: Deferred.** Depends on H-03–H-06 and a production consumer. No service API, remote Worker or
PostgreSQL code is currently part of the runtime.

### P-04 / P-05 — Web and desktop clients

**Status: Deferred.** The protocol is client-neutral, but implementation waits until the TUI proves
event/replay and approval contracts in sustained local use.

### Known follow-ups

These are bounded improvements, not permission to expand the current slice:

- Generate nested parent/child delegation compatibility corpus and recursive graph replay coverage.
- Document provider credentials and catalog configuration in package examples.
- Add long-session and reconnect soak tests for the Worker/TUI boundary.
- Define an explicit release/versioning policy for future Python wheels and the protocol package.

## Definition of done

A task is complete only when all applicable items are true:

- The public contract and dependency owner are explicit.
- Event, error, retry, cancellation and security semantics are tested.
- Existing sessions, fixtures and installed-wheel consumers remain compatible, or migration is provided.
- Documentation and examples reflect the actual code paths.
- The smallest relevant unit, integration, packaging and UI tests pass.
- The change is committed as one reviewable slice; unrelated cleanup is not mixed in.

## Development workflow

1. Start with the package boundary and acceptance criteria in this document.
2. Add or update contract/security tests before implementation.
3. Implement through public injection points; do not import private modules across packages.
4. Update the relevant README and [ARCHITECTURE.md](ARCHITECTURE.md) in the same slice.
5. Run focused tests, then the full quality gates.
6. Review the diff for generated files, credentials, local fixtures and accidental protocol changes.

For application features, keep product choices in aihi-code-agent or the CLI. For reusable Harness
features, keep the aihi.models -> aihi.agent dependency direction and add a compatibility test.

## Quality gates

~~~bash
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
pnpm --dir apps/aihi-code-cli test
~~~

Packaging work must additionally build isolated wheels, install them without editable-path injection,
verify PEP 420 namespace and py.typed, and replay frozen event/SQLite/trace fixtures. TUI work must
cover both reducer/replay behavior and the command/picker surface.

## Backlog rules

1. Keep this file as the single roadmap; do not create a second task list in a package.
2. Product-specific requests stay in the application until a second product demonstrates a reusable,
   provider-neutral need.
3. A platform feature must consume an existing EventStore, TelemetrySink or SandboxBackend protocol.
   If it requires changing Runtime semantics, stop and review the boundary first.
4. Security defaults are never relaxed for convenience: Host remains explicitly unsafe, ASK suspends,
   and side effects remain on the governed tool chain.
5. Keep ADR/RFC drafts local. Once a decision is stable, record the resulting contract here and in code
   tests rather than relying on an unpublished document.
