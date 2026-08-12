# @aihi/code-cli

TypeScript-side transport and Ink TUI for the local AIHI Code Worker. It
launches the Python worker, performs the versioned `initialize` handshake,
receives `event` notifications, and exposes the Coding Agent run loop.

The current local wire contract is protocol `0.2`. It requires an exact-version
handshake: a `0.1` CLI and `0.2` Worker intentionally refuse to connect instead
of guessing across incompatible Run acknowledgement shapes.

The worker uses JSON-RPC 2.0 envelopes framed as:

```text
Content-Length: <UTF-8 byte length>\r\n
\r\n
<JSON object>
```

The worker must be installed in the selected Python environment, or callers can
override `command`, `args`, `cwd`, and `env` in `RpcClient.connect()`. Set
`storePath` to use the Worker's SQLite event store; when omitted, the Worker
uses an in-memory store for the process lifetime. The `aihi-code` executable
always supplies `~/.aihi/sessions.sqlite3` by default, so normal CLI sessions
survive process restarts.

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
for reconnecting a TUI from a known sequence number. Replay and live durable
notifications feed the same Transcript projector: user and assistant messages,
Tool lifecycle, Approval state, and Run failures therefore render identically
before and after reconnect. Tool previews use an allowlist of display fields;
arbitrary Tool inputs are never serialized into the terminal transcript, and
credential-shaped substrings in allowlisted text are redacted.

After installing the TypeScript dependencies and building, start the local TUI
with:

```bash
npm run build
aihi-code
```

Launching starts a **new** session unless `--continue` or `--session` is used.
Any bare words after the options are the first turn, so a one-shot run is just:

```bash
aihi-code summarize the auth module
```

On exit the CLI prints the closed session's id and the command that reopens it
(`aihi-code --session SESSION_ID`). `--continue` (or `-c`) reopens the newest
session belonging to the selected workspace. `--store` remains available for
tests or advanced isolation of the event database.

`--workspace PATH` selects the project workspace and is an alias for `--cwd PATH`.
When `sandbox.root` is omitted, the Worker uses this workspace as the sandbox root;
an explicit `sandbox.root` can still restrict execution to another directory.

Configuration directories are fixed: the Worker checks
`~/.aihi/aihi-code.toml`, the legacy project-root `aihi-code.toml`, and
`<workspace>/.aihi/aihi-code.toml`. Existing layers are deep-merged in that
low-to-high precedence order; arrays are replaced by the higher layer. The CLI,
Worker RPC, and Worker environment do not accept a configuration-path override.
Relative paths are anchored to the file that declares them before layers are
merged. Configure the provider, sandbox, Skill roots, and MCP servers there;
credentials are referenced by environment variable name and are never stored
in TOML:

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

The generated user configuration keeps `sandbox.unsafe = false`. On first use
of Host mode, the TUI explains that Host execution is not isolated and asks the
user to trust the exact workspace and resolved Host execution root. That
acknowledgement is stored in the fixed user file `~/.aihi/host-workspaces.json`;
trusting one workspace does not trust another, and changing `sandbox.root`
requires confirmation again. Setting `unsafe = true` in configuration remains
the explicit non-interactive opt-in.

Use `--session SESSION_ID` to reopen a known session on startup, or `--continue`
to reopen the newest session for the workspace. `/sessions`
selects the newest persisted session when no session is specified; `/open` can
switch to another one. `/runs` lists run states, `/history` reloads the event
history, `/fork [SEQ]` creates a branch, and `/cancel RUN_ID` requests
cancellation of an active run or closes a suspended/recoverable run. Model
chunks are delivered as ephemeral Worker notifications and rendered while the
run is in progress. The canonical `assistant.message` replaces that temporary
stream display; ephemeral chunks are never inserted into the durable transcript.
The transcript viewport follows the newest line by default. `PageUp`/`PageDown`
scroll it without changing the underlying Event projection, `Ctrl-E` resumes
tail following, and `Ctrl-O` expands or collapses Tool result details. Its line
budget tracks terminal resize events instead of keeping a fixed entry count.
Only one foreground Run may own a Session. While it is active the composer is
disabled; `Ctrl-C` requests interruption of that Run, while `Ctrl-C` with no
active Run exits the CLI. Different Sessions may still run concurrently in the
Worker.
The composer supports pasted or `Ctrl-J`-inserted multiline prompts, local
command history with `Up`/`Down`, and slash-command suggestions completed with
`Tab` or `Shift-Tab`. `Enter` submits the full draft; command history remains
process-local and is not another durable Session store.
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
approval never auto-resumes inside the Worker. The TUI resumes after both an
approval and a denial so the model receives the decision as its ToolResult.
Approval prompts show a bounded, credential-redacted preview of the proposed
command or file change together with required capabilities, reason, and sandbox
context. Use `/skills` and `/skill-trust NAME` to inspect and explicitly trust
Skill hashes. Skill bodies remain explicit/trusted, and MCP tools enter the same
registry/policy chain as built-in tools.
