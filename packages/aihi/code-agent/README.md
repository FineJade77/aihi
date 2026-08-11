# aihi-code-agent

Coding Agent domain runtime for AIHI.

This package is intentionally UI-free. It owns Coding Agent configuration,
provider/sandbox/tool composition and the application-layer run entrypoints;
the recoverable, provider-neutral Agent Runtime remains in `aihi-agent`.

The TypeScript TUI communicates with this package through the versioned
`aihi-code-protocol` RPC boundary. The Worker supports Session/Task lifecycle
commands, `run.start`/`run.resume`, Worker-owned approval list/resolve commands,
and Skill list/trust commands. TOML config can declare Skill roots and MCP
stdio servers without putting credentials in the file. Skill body loading is
exposed as an explicit, trust-checked `load_skill` Tool when enabled.
Provider profiles can be declared as `[providers.<name>]`; `config.get` exposes
only non-secret metadata, and `run.start` accepts a configured provider/model
selection. Resume continues to enforce the persisted run configuration.
The application adds read-only `git_status`/`git_diff` tools and exposes
`skill.untrust`, `mcp.list`, and `tool.list` through the Worker.
