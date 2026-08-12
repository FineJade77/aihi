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
- `session.fork`
- `task.create`, `task.spawn`, `task.get`, `task.list`, `task.transition`
- `run.start`, `run.resume`, `run.list`, `run.cancel`
- `approval.list`, `approval.resolve`
- `skill.list`, `skill.trust`, `skill.untrust`
- `config.get`, `mcp.list`, `tool.list`

Mutating commands append canonical events in the Worker and the same events are
sent to the CLI as `event` notifications. `session.events` is the replay path
for reconnecting a TUI from a known sequence number.

After installing the TypeScript dependencies and building, start the local TUI
with:

```bash
npm run build
aihi-code --store ~/.aihi/code-agent/events.sqlite3
```

Launching always starts a **new** session. Any bare words after the options are
the first turn, so a one-shot run is just:

```bash
aihi-code --store ~/.aihi/code-agent/events.sqlite3 summarize the auth module
```

On exit the CLI prints the closed session's id and the command that reopens it
(`aihi-code --session SESSION_ID`). Without `--store` the session lives only in
the Worker process, so the hint says so rather than offering a resume that
cannot work.

`--workspace PATH` selects the project workspace and is an alias for `--cwd PATH`.
When `sandbox.root` is omitted, the Worker uses this workspace as the sandbox root;
an explicit `sandbox.root` can still restrict execution to another directory.

Configuration directories are fixed: the Worker checks
`<workspace>/.aihi/aihi-code.toml`, the legacy project-root `aihi-code.toml`,
and then `~/.aihi/aihi-code.toml` in that order. The CLI does not accept a
configuration-path argument. Relative paths in a discovered file are resolved
from the file's directory. Configure the provider, sandbox, Skill roots, and
MCP servers there; credentials are referenced by environment variable name and
are never stored in TOML:

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
unsafe = true

[artifacts]
enabled = true
path = "artifacts"

[agent]
compact_model = "deepseek-chat"
context_window = 128000

[subagents]
enabled = false
model = "deepseek-chat"
capabilities = ["filesystem.read"]

[[skills.roots]]
path = "skills"
scope = "project"

[skills]
load_tool = true
trust_lockfile = "skills.lock.json"

[mcp.servers.example]
command = ["python3", "-m", "example_mcp_server"]
allowed_tools = ["search"]
```

Use `--session SESSION_ID` to reopen a known session on startup. `/sessions`
selects the newest persisted session when no session is specified; `/open` can
switch to another one. `/runs` lists run states, `/history` reloads the event
history, `/fork [SEQ]` creates a branch, and `/cancel RUN_ID` requests
cancellation of an active run or closes a suspended/recoverable run. Model
chunks are delivered as ephemeral Worker notifications and rendered while the
run is in progress.
The default Coding tool set also includes read-only `git_status` and `git_diff`;
they never stage or modify changes and remain subject to the same Tool policy
chain.

After `/new`, ordinary input is a user turn and runs the Coding Agent loop.
Use `/provider NAME [MODEL]` and `/model MODEL` to choose a configured Provider
profile and model for subsequent new Runs; `/config` shows the effective,
non-secret configuration. Provider profiles are declared under
`[providers.<name>]`, while `[provider]` remains the default.
Use `/mcp` and `/tools` to inspect configured integrations, and
`/skill-disable NAME` or `/skill-untrust NAME` to remove a Skill's active trust.
`/run MESSAGE` is an explicit equivalent; `/resume RUN_ID` continues an
interrupted or approval-suspended run. Use `/approvals`, `/approve ID [once]`,
and `/deny ID` to operate the Worker-owned approval projection; resolving an
approval never auto-resumes a run. Use `/skills` and `/skill-trust NAME` to
inspect and explicitly trust Skill hashes. Skill bodies remain explicit/trusted,
and MCP tools enter the same registry/policy chain as built-in tools.
