# @aihi/code-cli

TypeScript-side transport for the local AIHI Code Worker. The package currently
provides the lifecycle bridge only: it launches the Python worker, performs the
versioned `initialize` handshake, receives `event` notifications, and performs a
graceful `shutdown`.

The worker uses JSON-RPC 2.0 envelopes framed as:

```text
Content-Length: <UTF-8 byte length>\r\n
\r\n
<JSON object>
```

The worker must be installed in the selected Python environment, or callers can
override `command`, `args`, `cwd`, and `env` in `RpcClient.connect()`. Set
`storePath` to use the Worker's SQLite event store; when omitted, the Worker
uses an in-memory store for the process lifetime.

The current Worker command surface is deliberately small:

- `session.create`, `session.list`, `session.get`, `session.events`
- `task.create`, `task.spawn`, `task.get`, `task.list`, `task.transition`

Mutating commands append canonical events in the Worker and the same events are
sent to the CLI as `event` notifications. `session.events` is the replay path
for reconnecting a TUI from a known sequence number.

After installing the TypeScript dependencies and building, start the local TUI
with:

```bash
npm run build
npm start -- --store ~/.aihi/code-agent/events.sqlite3
```

The first TUI provides session discovery, session creation/opening, event
replay, and basic task lifecycle commands. It intentionally does not execute
tools or model runs yet.
