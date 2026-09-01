# `aihi_code_agent` cache/compaction evaluation v1

This deterministic PR/release gate runs the same long-session task twice: once with enough context
capacity to retain the raw history and once at the rolling-compaction watermark. Both attempts must
pass the workspace and Harness oracles. The comparison additionally requires 100% critical-state
recall, the same hashed cache family, no in-task cache-key drift, a reported cache hit, at least one
ContextState v2 rolling compaction and fewer Provider-reported input tokens after compaction.

The history is 121 messages from one user request followed by 60 closed Tool Call/Result exchanges.
This specifically guards against treating a complete coding run as one uncompactable user turn.

The evaluator uses the packaged Coding Agent prompt and the provider-neutral Runtime with a scripted
Fake Provider. It proves the deterministic cache/compaction/report chain; it is not a real-model
capability or latency baseline. Wall-clock latency is recorded for diagnosis but is not used as a
regression gate.
