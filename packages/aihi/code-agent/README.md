# aihi-code-agent

Coding Agent domain runtime for AIHI.

This package is intentionally UI-free. It will own Coding Task phases, project
context, coding tools, verification and the application-layer model gateway;
the reusable Agent Runtime remains in `aihi-agent`.

The TypeScript TUI communicates with this package through the versioned
`aihi-code-protocol` RPC boundary. The Worker and RPC server are introduced in
a later implementation stage.
