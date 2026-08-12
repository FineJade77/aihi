# aihi-code-agent

Coding Agent domain runtime for AIHI.

This package is intentionally UI-free. It owns Coding Agent configuration,
provider/sandbox/tool composition and the application-layer run entrypoints;
the recoverable, provider-neutral Agent Runtime remains in `aihi-agent`.

The TypeScript TUI communicates with this package through the versioned
`aihi-code-protocol` 0.2 RPC boundary with an exact-version handshake. The Worker supports Session/Task lifecycle
commands, `run.start`/`run.resume`, Worker-owned approval list/resolve commands,
and Skill list/trust commands. TOML config can declare Skill roots and MCP
stdio servers without putting credentials in the file. Skill body loading is
exposed through `load_skill`, enabled by default because packaged Skills are
always present. Setting `skills.load_tool = false` removes both the Tool and its
model-facing index unless `load_skill` is explicitly listed in `agent.tools`.
Provider profiles can be declared as `[providers.<name>]`; `config.get` exposes
only non-secret metadata, and `run.start` accepts a configured provider/model
selection. Resume continues to enforce the persisted run configuration.
The default Tool authorization policy is configured with
`[agent].permission_mode`: `default`, `accept_edits`, `plan`, or `bypass`.
The value is persisted with each Run's startup configuration and hard safety
denies remain active in every mode.
Worker configuration locations are fixed and cannot be replaced by initialize
parameters or environment variables. User, legacy project-root, and
`<workspace>/.aihi` TOML files are merged in increasing precedence; relative
paths retain the declaring file as their base.
The generated Host configuration is fail-closed. Interactive acknowledgement
is persisted for the exact workspace and resolved execution root in
`~/.aihi/host-workspaces.json`; changing `sandbox.root` requires confirmation
again. Configuration may still set `sandbox.unsafe = true` for an explicit
non-interactive opt-in.
The application adds read-only `git_status`/`git_diff` tools and exposes
`skill.untrust`, `mcp.list`, and `tool.list` through the Worker.
Configuration can opt into the Harness artifact store, model-driven context
compaction, and governed subagents; subagents require a Worker Session store
and inherit a read-only capability ceiling unless explicitly narrowed or
expanded by application configuration.

Each run writes redacted, bounded event observations to `audit.jsonl` by
default. The path is relative to the declaring TOML file (the generated user
config therefore uses `~/.aihi/audit.jsonl`; a project config uses
`<workspace>/.aihi/audit.jsonl`). Disable it with `[audit] enabled = false`, or
set `[audit] path = "..."` to choose another file. The file is append-only and
contains no raw credentials; the underlying Agent Telemetry redactor bounds
and sanitizes event payloads before writing. The file is created with owner-only
permissions (`0600`); `/doctor` reports whether the existing file or its nearest
existing parent directory is writable without creating the file.

## 行为变更（RFC-0003 领域层）

- **提示词由应用拥有，不可配置**。`agent.system_prompt` 与 `agent.system_prompt_mode`
  已移除，配置里出现会直接报错。`run.start` / `run.resume` 也不再接受 `system_prompt` 参数。
  调整 Agent 行为请改 `aihi/code_agent/prompts/coding.md`；项目特有规则写进工作区的
  `AGENTS.md`（自动注入）。
- `BUILTIN` 作用域的 Skill 随包发布并隐式受信，不需要 trust lockfile。
  `USER` / `PROJECT` / `WORKSPACE` 作用域的显式信任要求不变。
- 新增命名 Subagent 类型（`explore`、`code_review`、`test`、`general`），
  经 `task` 工具的 `agent_type` 字段选择，各自带专属提示词。
  `[subagents]` 现在默认开启，授权收敛为只读（`max_depth = 1`、`max_children = 3`、
  仅 `filesystem.read`）。`CodeAgentRuntime.create()` 的 `store` 参数改为必填——子会话
  必须落在父会话同一个 store 里，联合回放（ADR-0027）才成立。

## Run 提交是非阻塞的

`run.start` 与 `run.resume` 在 Worker **受理**后立即返回 `{run_id, accepted}`，不等运行结束。
运行结果由事件送达：`run.completed` / `run.failed` / `run.interrupted` / `run.cancelled`，
审批则是 `approval.requested`。

这样客户端的请求超时只覆盖真正快速的命令，不会再压在模型的思考时间上——此前一次超过
客户端超时的编码运行会让请求失败，而 Worker 仍在后台执行。

若一个 Run 在开始前就失败（例如 resume 一个不存在的 run），它不会产生任何终态事件，
Worker 会发出 `run.error` 通知携带 `session_id`、`run_id` 与原因；这是此类失败唯一的客户端
可见信号。

stdio transport 通过 `RunSupervisor` 保证同一个 Session 同时最多只有一个 foreground
Run，不同 Session 仍可并行。取消信号按 `(session_id, run_id)` 路由，后台产生的事件通过
线程安全队列交给 stdout 主线程，避免并发 append/drain 丢失通知。

TUI 在有待审批项时由审批提示接管输入行，展示经过长度限制和凭据脱敏的 Tool input，以及
capabilities、reason 和 sandbox，并由 `y` / `o`（仅此次）/ `n` 单键解决。批准或拒绝后 CLI
都会紧接着调用 `run.resume`：批准后执行 Tool，拒绝后把 permission-denied ToolResult 返回给
模型。Worker 的「resolve 从不自动 resume」不变式没有改变，是客户端把这两步合成一次操作。
