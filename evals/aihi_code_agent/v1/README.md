# `aihi_code_agent` benchmark v1

This version contains nine deterministic smoke-plus tasks covering bug fixing,
feature implementation, test repair, a security boundary, refactoring,
repository understanding, instruction following, interruption/resume and
Subagent planning. The manifest pins fixture hashes and hidden-style oracle
commands; `baseline.json` records the scripted reference executor result. This
baseline validates the runner/oracle chain and is explicitly not a real-model
capability score.

The first reviewed real-Provider baseline is
[`baselines/deepseek-v4-flash-2026-08-17.json`](baselines/deepseek-v4-flash-2026-08-17.json).
DeepSeek `deepseek-v4-flash` passed 26 of 27 attempts across nine tasks repeated
three times: empirical pass@1 was 96.3%, every base task passed at least once,
stable pass rate was 88.9%, and task latency was P50 16.0 seconds / P95 59.7
seconds. The one failed attempt reached the 90-second task limit and was
durably recorded as `INTERRUPTED`; it was reviewed as `execution_timeout`.

`nightly.config.example.toml` documents the required real-Provider, credential
environment and Docker/no-network settings; it is a template and is not
usable until its model and environment are supplied outside the repository.
Each v1 task allows up to 90 seconds for a live Provider run; this bounds the
end-to-end task, including model/tool round trips, rather than only the oracle.

Run a repeated multi-model benchmark from the repository root:

```bash
python3 -m scripts.evals.run --mode nightly \
  --config /secure/model-a.toml \
  --config /secure/model-b.toml \
  --repeat 3 \
  --output eval-results/nightly
```

`live-summary.json` compares empirical pass@1, stable pass rate, P50/P95 task
latency, tokens, model/tool calls and Provider-reported cost. Configuration
paths, credentials and raw model/tool output are not included. Generated live
reports remain local or CI artifacts by default; publish a report only after
reviewing its model name, metrics and redaction boundary.

PR mode compares only with the scripted reference baseline. Nightly/release
automatically select a reviewed baseline with the same Provider and Model, then
fail when a paired bootstrap separates the pass@1 drop from sampling noise, or
when a base case that used to pass every attempt now fails every attempt. A
smaller drop is reported as a warning. `baselines/*.json` therefore records
per-case attempt counts, and the oracle commands of a live run are graded in a
disposable container rather than on the host. A new model without a reviewed baseline is
reported as `baseline unavailable` and must pass every attempt; its generated
report can then be reviewed and versioned separately. `--baseline` explicitly
selects a baseline when a one-profile run needs an override.
