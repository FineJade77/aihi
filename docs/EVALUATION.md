# AIHI Evaluation Contract v1

This document freezes the first evaluation boundary. It defines data ownership,
case formats, release semantics and the local automation gates.

## Two datasets, one report contract

`aihi-agent` and `aihi-code-agent` are evaluated together but not by the same
oracle:

| Dataset | Owner | Input | Primary oracle | Release rule |
| --- | --- | --- | --- | --- |
| `aihi-agent-conformance-v1` | `aihi-agent` | Redacted `TraceBundle` | Replay, event and security invariants | 100% required |
| `aihi-code-agent-benchmark-v1` | `aihi-code-agent` | Isolated workspace and task prompt | Hidden tests, regression, scope and safety | Compare pass@1 baseline |

One Coding Agent run may produce both a product result and a redacted Harness
trace. The two scores remain separate so a correct patch cannot hide a runtime
contract violation.

The canonical data directories are:

```text
evals/
├── schemas/
├── aihi_agent/v1/
└── aihi_code_agent/v1/
```

The JSON Schemas in `evals/schemas/` are the serialization contract:

- `harness-case.schema.json` describes replay-only valid and rejected cases.
- `code-task.schema.json` describes an isolated Coding Agent task and its oracle.
- `eval-report.schema.json` describes the stable machine-readable result.

## Harness conformance cases

Cases are deterministic and must not invoke a Provider, Tool or Sandbox. The
corpus covers lifecycle ordering, tool/result pairing, approval suspension and
resume, interruption/cancellation, sequence integrity, child authority and
redaction. It includes both valid traces and intentionally invalid traces with a
stable expected error code.

The conformance gate is binary: every required case must pass. Scores such as
0.8 or 0.9 are useful for diagnosis but never substitute for the release gate.

## Coding Agent benchmark cases

Each task pins a fixture SHA-256, execution limits and an oracle. The default
release profile is Docker with networking disabled. A task is successful only
when all required conditions pass:

```text
hidden tests
AND regression tests (when required)
AND allowed/forbidden path checks
AND Harness safety/conformance checks
```

The v1 smoke-plus corpus currently contains nine tasks covering bug fixes, small
features, test repair, a security boundary, refactoring, repository
understanding, instruction following, interruption/resume and subagent use. The
committed scripted baseline checks the runner/oracle chain only; it is explicitly
not a real-model capability score.

## Reproducibility and compatibility

- Dataset versions are immutable once used as a baseline.
- Fixtures and hidden oracles are not rewritten to fit an implementation.
- Reports are strict JSON and contain provider/model and prompt/tool hashes when
  a live model run is used.
- Raw credentials and unredacted model/tool output never enter the corpus or
  committed reports.
- A schema or semantic change requires a new version rather than silently
  changing an existing case.

## Execution modes

- `offline`: replay-only Harness conformance; no external calls.
- `pr`: Harness corpus plus a small deterministic Coding Agent smoke set.
- `nightly`: full benchmark, repeated runs and baseline comparison.
- `release`: the same as nightly with the release gate applied.

Run the local gate with:

```bash
python3 -m scripts.evals.run --mode offline
python3 -m scripts.evals.run --mode pr
```

Reports are written below `eval-results/<mode>/`. Exit code `0` means the gate
passed, `1` means an evaluation case failed, and `2` means setup or
configuration failed. `nightly` and `release` require an explicit
`--config <path>` containing a real Provider, its `api_key_env`, a Docker image,
and networking disabled; fake Provider, MCP servers, missing credentials and
placeholder models fail closed. The committed
`evals/aihi_code_agent/v1/nightly.config.example.toml` is a credential-free
template. These modes also write `baseline-comparison.json`; the baseline is a
diagnostic comparison while the release gate still requires every task to
pass. The CI template in
`.github/workflows/evals.yml` runs `pr` on pull requests and exposes the other
modes through an explicit dispatch input, without storing credentials.
