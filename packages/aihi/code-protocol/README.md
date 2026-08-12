# @aihi/code-protocol

Versioned TypeScript DTOs and JSON Schemas for the AIHI coding-agent Worker boundary.

This private workspace package is the language-neutral contract between the Python `aihi-code-agent` Worker and the TypeScript `@aihi/code-cli` client. It defines data and compatibility rules; it does not start a Worker, execute tools, or contain business policy.

## Responsibilities

- JSON-RPC 2.0 request, response, notification, and error types.
- Agent, session, task, run, approval, Skill, MCP, tool, and configuration DTOs.
- Protocol version and exact handshake constants.
- Checked-in JSON Schemas for envelopes and high-value notifications.
- TypeScript compile-time contracts shared by clients and protocol tests.

The Worker implementation and Content-Length framing live in `aihi-code-agent` and the CLI RPC client respectively. Keep this package free of runtime side effects so another host can implement the same protocol.

## Compatibility

The current protocol version is `0.2`. Clients and Workers perform an exact-version handshake; a mismatch must be surfaced as a compatibility error rather than silently downgraded.

Transport messages are JSON-RPC 2.0 objects framed with a `Content-Length` header over a byte stream. The framing is deliberately separate from the DTO package so transports other than stdio can reuse the same payloads.

## Command groups

| Group | Methods |
| --- | --- |
| Sessions | `session.create`, `session.list`, `session.get`, `session.events`, `session.fork`, `session.usage` |
| Tasks | `task.create`, `task.spawn`, `task.get`, `task.list`, `task.transition` |
| Runs | `run.start`, `run.resume`, `run.list`, `run.cancel` |
| Approvals | `approval.list`, `approval.resolve` |
| Skills | `skill.list`, `skill.trust`, `skill.untrust` |
| Configuration | `config.get`, `config.init`, `config.acknowledge_host` |
| Integrations | `mcp.list`, `tool.list` |

`run.start` and `run.resume` are asynchronous acceptance commands. Terminal state is delivered through notifications such as `run.completed`, `run.failed`, `run.interrupted`, `run.cancelled`, `run.error`, and `approval.requested`.

## Usage

```ts
import {
  PROTOCOL_VERSION,
  type ConfigDescriptor,
  type CodeRpcMethod,
} from "@aihi/code-protocol";

const version = PROTOCOL_VERSION; // "0.2"
const method: CodeRpcMethod = "run.start";

function showConfig(config: ConfigDescriptor) {
  console.log(config.workspace, config.provider, config.audit);
}
```

The package exports its TypeScript entrypoint as `@aihi/code-protocol` and checked-in schemas under `@aihi/code-protocol/schema/*`.

## Repository layout

```text
src/index.ts                         DTOs, unions, constants, and type guards
schema/rpc-envelope.schema.json      JSON-RPC envelope
schema/event-notification.schema.json event stream notifications
schema/run-accepted.schema.json      asynchronous run acceptance
schema/run-error-notification.schema.json pre-run errors
schema/approval-descriptor.schema.json approval payloads
```

## Development

```bash
pnpm --filter @aihi/code-protocol typecheck
pnpm --filter @aihi/code-protocol build
```

The package is currently `private` and is consumed through the workspace. Any protocol change should update the TypeScript unions, the corresponding schema, Worker behavior, CLI behavior, and contract tests together.

## Related packages

- [`aihi-code-agent`](../code-agent/README.md) implements the Python Worker.
- [`@aihi/code-cli`](../../../apps/aihi-code-cli/README.md) is the local TUI client.
- [Repository architecture](../../../docs/ARCHITECTURE.md)
