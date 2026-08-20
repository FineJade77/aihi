# AIHI evaluation data

This directory contains versioned evaluation specifications and datasets. It is
not part of any Python wheel or runtime package.

The dataset families have different owners:

- `aihi_agent/` contains provider-neutral Harness conformance cases. These are
  redacted event traces evaluated by offline replay.
- `aihi_code_agent/` contains Coding Agent tasks. These run in an isolated
  workspace and are graded with hidden tests, scope checks and the exported
  Harness trace. `v1/` is the capability benchmark; `context-v1/` is the paired
  long-session cache/compaction gate.

Schemas are in `schemas/`. The contract is documented in
[`docs/EVALUATION.md`](../docs/EVALUATION.md) and
[`docs/EVALUATION.zh-CN.md`](../docs/EVALUATION.zh-CN.md).

Dataset directories are versioned (`v1`, `v2`, ...), optionally with a purpose
prefix such as `context-v1`. A fixture or schema change
that changes the meaning of an existing case requires a new dataset version;
do not rewrite a frozen case to make an implementation pass.
