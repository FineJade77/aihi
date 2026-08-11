# @aihi/code-protocol

Language-neutral RPC schemas and TypeScript types for the TypeScript
`aihi-code-cli` and Python `aihi-code-agent` Worker boundary.

The protocol uses JSON-RPC 2.0 envelopes framed with `Content-Length` headers.
Durable Agent events carry a session sequence; ephemeral stream events are
best-effort UI data and never replace the persisted event log.

The Worker advertises the available command descriptors in the `initialize`
result. The current `0.1` command set is:

```text
session.create   session.list   session.get   session.events
task.create      task.spawn    task.get       task.list   task.transition
```

All mutating commands are handled by the Python Worker. The CLI can recover a
session by calling `session.events` with `after_seq`, then continue consuming
`event` notifications. The protocol carries JSON DTOs only; it never exposes
Python runtime objects.
