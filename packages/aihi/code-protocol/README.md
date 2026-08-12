# @aihi/code-protocol

Language-neutral RPC schemas and TypeScript types for the TypeScript
`aihi-code-cli` and Python `aihi-code-agent` Worker boundary.

The protocol uses JSON-RPC 2.0 envelopes framed with `Content-Length` headers.
Durable Agent events carry a session sequence; ephemeral stream events are
best-effort UI data and never replace the persisted event log.

The Worker advertises the available command descriptors in the `initialize`
result. Protocol `0.2` uses an exact-version handshake and publishes:

```text
session.create/list/get/events/fork/usage
task.create/spawn/get/list/transition
run.start/resume/list/cancel
approval.list/resolve
skill.list/trust/untrust
config.get/init/acknowledge_host
mcp.list   tool.list
```

`run.start` and `run.resume` return a non-blocking acknowledgement with a
required `run_id`; progress and terminal state arrive through `event`
notifications. If an acknowledged Run fails before it can produce a canonical
Run event, the Worker emits `run.error` with both `session_id` and `run_id`.

All mutating commands are handled by the Python Worker. The CLI recovers a
session by following `session.events(after_seq)` until `has_more` is false, then
continues consuming `event` notifications. TypeScript request/result mappings
and runtime guards live in this package beside the language-neutral JSON
schemas. The protocol carries JSON DTOs only; it never exposes Python runtime
objects.
