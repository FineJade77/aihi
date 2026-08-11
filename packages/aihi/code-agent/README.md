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

- `agent.system_prompt` 现在**追加**在内置 coding 提示词之后，而不是替换它。
  需要完全替换时设置 `agent.system_prompt_mode = "replace"`。
- `BUILTIN` 作用域的 Skill 随包发布并隐式受信，不需要 trust lockfile。
  `USER` / `PROJECT` / `WORKSPACE` 作用域的显式信任要求不变。
- 新增命名 Subagent 类型（`explore`、`code_review`、`test`、`general`），
  经 `task` 工具的 `agent_type` 字段选择，各自带专属提示词。
  `[subagents]` 仍默认关闭，且启用时需要向 `CodeAgentRuntime.create()` 传入 EventStore。
