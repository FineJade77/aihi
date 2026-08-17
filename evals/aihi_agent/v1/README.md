# `aihi_agent` conformance corpus v1

This version contains valid and intentionally invalid redacted TraceBundle
cases in `manifest.jsonl`. It covers completion/failure, approval suspension
and resume, interruption with a pending tool, cancellation, redaction,
sequence/identity integrity and terminal-state payload integrity. Cases are
evaluated with `HarnessConformanceRunner` and are safe to run offline. The
corpus will grow by adding cases without changing the meaning of these frozen
entries.
