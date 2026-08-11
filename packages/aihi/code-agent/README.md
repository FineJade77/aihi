# aihi-code-agent

Coding Agent domain runtime for AIHI.

This package is intentionally UI-free. It owns Coding Agent configuration,
provider/sandbox/tool composition and the application-layer run entrypoints;
the recoverable, provider-neutral Agent Runtime remains in `aihi-agent`.

The TypeScript TUI communicates with this package through the versioned
`aihi-code-protocol` RPC boundary. The Worker supports Session/Task lifecycle
commands plus `run.start` and `run.resume`; TOML config can declare Skill roots
and MCP stdio servers without putting credentials in the file.
