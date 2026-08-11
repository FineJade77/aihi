# @aihi/code-cli

TypeScript-side transport and Ink TUI for the local AIHI Code Worker. It
launches the Python worker, performs the versioned `initialize` handshake,
receives `event` notifications, and exposes the Coding Agent run loop.

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
- `run.start`, `run.resume`

Mutating commands append canonical events in the Worker and the same events are
sent to the CLI as `event` notifications. `session.events` is the replay path
for reconnecting a TUI from a known sequence number.

After installing the TypeScript dependencies and building, start the local TUI
with:

```bash
npm run build
npm start -- --store ~/.aihi/code-agent/events.sqlite3
```

Use `--config PATH` (or a project `aihi-code.toml`) to configure a provider,
sandbox, Skill roots, and MCP servers. Credentials are referenced by environment
variable name and are never stored in TOML:

```toml
[provider]
name = "deepseek"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[sandbox]
backend = "host"
root = "."
unsafe = true

[[skills.roots]]
path = ".aihi/skills"
scope = "project"

[mcp.servers.example]
command = ["python3", "-m", "example_mcp_server"]
allowed_tools = ["search"]
```

After `/new`, ordinary input is a user turn and runs the Coding Agent loop.
`/run MESSAGE` is an explicit equivalent; `/resume RUN_ID` continues an
interrupted or approval-suspended run. Skill bodies remain explicit/trusted,
and MCP tools enter the same registry/policy chain as built-in tools.
