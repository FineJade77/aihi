# AIHI Evaluation Contract v1

This document freezes the first evaluation boundary. It defines data ownership,
case formats, release semantics and the local automation gates.

## Three datasets, one report contract

`aihi-agent` and `aihi-code-agent` are evaluated together but not by the same
oracle:

| Dataset | Owner | Input | Primary oracle | Release rule |
| --- | --- | --- | --- | --- |
| `aihi-agent-conformance-v1` | `aihi-agent` | Redacted `TraceBundle` | Replay, event and security invariants | 100% required |
| `aihi-code-agent-benchmark-v1` | `aihi-code-agent` | Isolated workspace and task prompt | Hidden tests, regression, scope and safety | Compare pass@1 baseline |
| `aihi-code-agent-context-v1` | `aihi-code-agent` | Paired deterministic long sessions | Cache/compaction invariants plus task outcome | 100% required |

One Coding Agent run may produce both a product result and a redacted Harness
trace. The two scores remain separate so a correct patch cannot hide a runtime
contract violation.

The canonical data directories are:

```text
evals/
├── schemas/
├── aihi_agent/v1/
├── aihi_code_agent/v1/
└── aihi_code_agent/context-v1/
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

### Oracle execution boundary

Provider-written code is never executed in the evaluation process. A live run
grades each oracle command in its own disposable container built from
`sandbox.image`, with no network, a read-only root filesystem, all capabilities
dropped and the task workspace as the only mounted host path. Configuring a live
run without `sandbox.image`, or injecting the host command executor into it,
fails closed. Every report records where grading happened in
`config.oracle_execution` (`docker:<image>`, `host` or `injected`).

The deterministic PR gate keeps host execution: its patches come from the
scripted reference executor committed to this repository, so no model output is
involved and the gate does not require a Docker daemon.

## Joint cache/compaction evaluation

`aihi-code-agent-context-v1` runs the same packaged-prompt task against an uncompacted long-session
baseline and a ContextState v2 hard-compaction profile. Both workspace outcomes and exported Harness
Traces must pass. The comparison additionally gates on 100% critical-state recall, identical hashed
cache-family identity, zero in-task cache-key changes, at least one hard compaction, a cache hit and
fewer input tokens after compaction. It records task latency but does not gate on wall-clock timing.

The Harness corpus also contains a replay-only `cache-compaction-v2` golden Trace with cache read/write
usage, stable-prefix identity, pressure metadata and a schema-v2 compaction record. This leaves the
existing Coding benchmark and its reviewed live baseline immutable while making the joint behavior a
required PR and release preflight.

## Reviewed live baseline

The first reviewed live result uses DeepSeek `deepseek-v4-flash` on 2026-08-17.
Across nine base tasks repeated three times, it passed 26/27 attempts: empirical
pass@1 was 96.3%, at-least-once success was 100%, stable pass rate was 88.9%,
and task latency was P50 16.0 seconds / P95 59.7 seconds. The one failed attempt
was interrupted at the 90-second task limit; the other two attempts for that
base task passed. The credential-free, prompt/tool-hashed artifact is stored at
[`evals/aihi_code_agent/v1/baselines/deepseek-v4-flash-2026-08-17.json`](../evals/aihi_code_agent/v1/baselines/deepseek-v4-flash-2026-08-17.json).
This result is a versioned project baseline for regression comparison, not a
claim about general model capability.

PR mode uses the scripted reference baseline only to verify the runner/oracle
chain. Nightly/release select a reviewed baseline whose Provider and Model both
match the live report. A profile without a reviewed baseline is labeled
`baseline unavailable` instead of falling back to the scripted score, and keeps
the strict all-attempt gate while producing an artifact for review. An explicit
`--baseline` overrides automatic selection for a single-profile comparison.

### Regression decision

A live `pass@1` is one sample of a stochastic run, so the reviewed-baseline gate
does not compare it against a stored number directly. It pairs the two profiles
by base case and runs a hierarchical bootstrap: cases are resampled with
replacement, and every drawn case resamples its own attempts. The gate fails
when either rule fires:

- the bootstrap interval for the `pass@1` delta lies entirely below zero **and**
  the drop is at least `--regression-margin` (default 0.05), or
- a base case that passed every baseline attempt now fails every attempt.

A drop that the interval cannot separate from sampling noise is reported as a
warning (a `::warning` annotation under GitHub Actions) and does not fail the
build. The second rule keeps a small corpus honest: requiring every repeat of a
case to fail means one flaky attempt cannot turn the gate red, while a genuinely
broken capability still does.

The decision is reproducible. `--bootstrap-resamples` (default 10000) and
`--bootstrap-seed` (default 20260817) are recorded together with the interval,
the margin and the paired case count in `baseline-comparison.json` under
`regression`.

A reviewed baseline therefore has to describe its per-case attempts. Artifacts
may record them directly in a `per_case` block; otherwise they are derived from
a uniform repetition count plus `reviewed_failures`. Either way the totals must
agree with the recorded summary, so a hand-edited baseline cannot weaken the
gate.

## Reproducibility and compatibility

- Dataset versions are immutable once used as a baseline.
- Fixtures and hidden oracles are not rewritten to fit an implementation.
- Reports are strict JSON and contain provider/model and prompt/tool hashes when
  a live model run is used. `prompt_sha256` fingerprints the packaged Coding
  prompt template; `tools_sha256` fingerprints the actual model-visible Tool
  definitions assembled for the run.
- Numeric cache/token/compaction metrics remain available after Trace redaction; credential-like keys
  such as `access_token` are still redacted. Full cache keys and prompts never enter reports.
- Raw credentials and unredacted model/tool output never enter the corpus or
  committed reports.
- A schema or semantic change requires a new version rather than silently
  changing an existing case.

## Execution modes

- `offline`: replay-only Harness conformance; no external calls.
- `pr`: Harness corpus, the deterministic cache/compaction comparison and the Coding Agent smoke set.
- `nightly`: full benchmark, repeated runs and baseline comparison.
- `release`: the same as nightly with the release gate applied.

`nightly` names a sampling profile, not a schedule. The live modes cost money
and need provider credentials, so nothing runs them automatically; they are
invoked by hand, locally or through `workflow_dispatch`. Run one after a change
to the Agent loop, prompts, tools or context handling, and before publishing a
release. A reviewed baseline stays valid until then, which also means it can be
older than the code it is compared against; `generated_at` in the artifact is
the date to check.

Run the local gate with:

```bash
python3 -m scripts.evals.run --mode offline
python3 -m scripts.evals.run --mode pr
python3 -m scripts.evals.run --mode nightly \
  --config /secure/model-a.toml \
  --config /secure/model-b.toml \
  --repeat 3 \
  --output eval-results/nightly
```

Reports are written below `eval-results/<mode>/`. Exit code `0` means the gate
passed, `1` means an evaluation case failed, and `2` means setup or
configuration failed. `nightly` and `release` require an explicit
`--config <path>` containing a real Provider, its `api_key_env`, a Docker image,
`access_mode = "full_access"`, `run_mode = "execute"`, and networking disabled; fake Provider, MCP
servers, missing credentials, placeholder models and interactive permission
modes fail closed. The committed
`evals/aihi_code_agent/v1/nightly.config.example.toml` is a credential-free
template. `nightly` and `release` default to three attempts per task; `--repeat`
overrides that value. Repeating `--config` evaluates multiple Provider/model
profiles only after every profile has passed fail-closed validation.

Each live case records task duration, model calls, tool calls, input/output/cache-read/cache-write
tokens, cache-hit ratio, cache-key changes, soft/hard compaction counts and Provider-reported cost when
available. The summary reports empirical
`pass_at_1` (the mean per-task success fraction), at-least-once success, stable
all-attempt success and P50/P95 task latency. Every non-offline gate writes `context.json` and
`context-comparison.json`. A single live profile writes `code.json` and `baseline-comparison.json`; a
matrix writes those live files below `profiles/` plus a credential-free `live-summary.json` for
comparison. The
scripted baseline remains a runner/oracle diagnostic and is never presented as
a model score. Reviewed live baselines are selected by exact Provider/Model
identity and govern pass@1 regression; unbaselined profiles never silently use
the scripted baseline.

The CI workflow in `.github/workflows/evals.yml` runs deterministic `pr` mode on
pull requests. Live modes are available only through `workflow_dispatch`: they
require provider credentials, so a scheduled job in a repository without those
secrets would fail every night without evaluating anything. A dispatched live
run without the secrets exits with the setup error rather than reporting
success. It reconstructs a mode-0600 TOML file from the
`AIHI_CODE_EVAL_CONFIGS_B64` repository secret and read Provider credentials
from the matching API-key secret. The secret contains one base64-encoded TOML
per non-empty line, so one dispatch can compare multiple models. The separate
`.github/workflows/ci.yml` workflow
runs Python 3.11/3.12 compile, Ruff, strict Mypy, the full test/packaging suite,
TypeScript type checks and the CLI build/tests.

Create the secret payload without including API-key values in the TOML:

```bash
base64 < /secure/model-a.toml | tr -d '\n'
base64 < /secure/model-b.toml | tr -d '\n'
```

Store the two resulting lines in `AIHI_CODE_EVAL_CONFIGS_B64`; store the actual
keys separately as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` or
`AIHI_CODE_AGENT_API_KEY` GitHub Secrets.
