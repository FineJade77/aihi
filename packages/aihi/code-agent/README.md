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
Configuration can opt into the Harness artifact store, model-driven context
compaction, and governed subagents; subagents require a Worker Session store
and inherit a read-only capability ceiling unless explicitly narrowed or
expanded by application configuration.

## 行为变更（RFC-0003 领域层）

- **提示词由应用拥有，不可配置**。`agent.system_prompt` 与 `agent.system_prompt_mode`
  已移除，配置里出现会直接报错。`run.start` / `run.resume` 也不再接受 `system_prompt` 参数。
  调整 Agent 行为请改 `aihi/code_agent/prompts/coding.md`；项目特有规则写进工作区的
  `AGENTS.md`（自动注入）。
- `BUILTIN` 作用域的 Skill 随包发布并隐式受信，不需要 trust lockfile。
  `USER` / `PROJECT` / `WORKSPACE` 作用域的显式信任要求不变。
- 新增命名 Subagent 类型（`explore`、`code_review`、`test`、`general`），
  经 `task` 工具的 `agent_type` 字段选择，各自带专属提示词。
  `[subagents]` 仍默认关闭，且启用时需要向 `CodeAgentRuntime.create()` 传入 EventStore。

## Run 提交是非阻塞的

`run.start` 与 `run.resume` 在 Worker **受理**后立即返回 `{run_id, accepted}`，不等运行结束。
运行结果由事件送达：`run.completed` / `run.failed` / `run.interrupted` / `run.cancelled`，
审批则是 `approval.requested`。

这样客户端的请求超时只覆盖真正快速的命令，不会再压在模型的思考时间上——此前一次超过
客户端超时的编码运行会让请求失败，而 Worker 仍在后台执行。
