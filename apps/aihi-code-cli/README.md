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
- `approval.list`, `approval.resolve`
- `skill.list`, `skill.trust`

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

[providers.openai]
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"

[sandbox]
backend = "host"
root = "."
unsafe = true

[[skills.roots]]
path = ".aihi/skills"
scope = "project"

[skills]
load_tool = true
trust_lockfile = ".aiharness/skills.lock.json"

[mcp.servers.example]
command = ["python3", "-m", "example_mcp_server"]
allowed_tools = ["search"]
```

Use `--session SESSION_ID` to reopen a known session on startup. `/sessions`
selects the newest persisted session when no session is specified; `/open` can
switch to another one. `/runs` lists run states, `/history` reloads the event
history, `/fork [SEQ]` creates a branch, and `/cancel RUN_ID` closes a
suspended or recoverable run.

After `/new`, ordinary input is a user turn and runs the Coding Agent loop.
Use `/provider NAME [MODEL]` and `/model MODEL` to choose a configured Provider
profile and model for subsequent new Runs; `/config` shows the effective,
non-secret configuration. Provider profiles are declared under
`[providers.<name>]`, while `[provider]` remains the default.
`/run MESSAGE` is an explicit equivalent; `/resume RUN_ID` continues an
interrupted or approval-suspended run. Use `/approvals`, `/approve ID [once]`,
and `/deny ID` to operate the Worker-owned approval projection; resolving an
approval never auto-resumes a run. Use `/skills` and `/skill-trust NAME` to
inspect and explicitly trust Skill hashes. Skill bodies remain explicit/trusted,
and MCP tools enter the same registry/policy chain as built-in tools.
