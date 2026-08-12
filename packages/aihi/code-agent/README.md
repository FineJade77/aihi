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
Worker 会发出 `run.error` 通知携带 `run_id` 与原因；这是此类失败唯一的客户端可见信号。

TUI 在有待审批项时由审批提示接管输入行，`y` / `o`（仅此次）/ `n` 单键解决。批准后 CLI
会紧接着调用 `run.resume`——Worker 的「resolve 从不自动 resume」不变式没有改变，是客户端
把这两步合成一次按键。
