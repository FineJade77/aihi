# ADR-0023：Subagent 通过工具链路执行，子 Run 独立成会话

状态：Accepted
日期：2026-08-07
关联：ADR-0022（Runtime 能力注入点）、ARCHITECTURE §11、TASK.md M6a / H-02

## 背景

`agents/` 有完整的治理原语 —— `TaskGraph` 校验 capability 子集、budget 子集、workspace 包含、
深度和兄弟数量上限，`Mailbox` 有界且需显式 ack，全部可快照恢复 —— 但**没有任何东西会执行子任务**。
包外零引用，从父 Run 到「跑起来一个权限受限的子 Run」之间没有路径。

这是 ADR-0022 之后最后一个未接线的能力，也是唯一一个不属于「上下文维度」的：它是派生子 Run。

## 决策

### 1. Subagent 是一个工具，不是 Runtime 的新分支

`SubagentTool`（工具名 `task`）注册进普通 `ToolRegistry`。因此派生子代理和任何其他副作用一样，
必须经过 `tools → policy → hooks → sandbox`：模型不能绕过 Policy 直接派生子 Run，
`task` 调用本身也会被审批、记录和取消。

工具只做两件事：用 `TaskGraph` 校验授权，然后把执行委托给注入的 `SubagentRunner`。
授权违规（能力越权、预算超限、深度或兄弟数超标）以稳定 error code 变成 Tool Result，
子 Run 根本不会启动。

`task` 声明 `required_capabilities=("agent.spawn",)` 且 `mutates=True`，所以：
默认模式需要显式 Approval；**Plan 模式直接拒绝派生**。

### 2. 子 Run 拥有独立 Session

子 Run 不写父 Session。原因是单写者不变式：工具运行在父 Run 的 dispatch 内部，
如果它从旁路 append 父事件流，父 `Session` 的 `head_seq` 立刻过期，
下一次 `expected_seq` 追加就会冲突并炸掉父 Run。

因此：

- 子 Session 的 metadata 记录 `parent_session_id`、`parent_run_id`、`task_id`、`depth`；
- `subagent.started` / `subagent.completed` 写入**子** Session；
- 父 Session 通过 `task` 工具的 Tool Result metadata（`session_id`、`run_id`、`task_id`、`state`）
  与子 Session 关联，这条记录由父 Runtime 正常持久化。

两侧日志都完整，且都只有一个写者。

### 3. 子 Run 的权限模式取父子中更严格的一方

`ToolContext` 新增 `permission_mode` 字段。`ChildRunSubagentRunner` 取
`min(配置的模式, 父 Run 当前模式)`（`plan < default < accept_edits < bypass`）。
否则一个配置为 `accept_edits` 的 runner 会让 `default` 模式的父 Run 通过委派获得更高权限 ——
这是提权。

### 4. 预算是真实生效的，不只是类型

- `timeout_seconds` → `asyncio.wait_for` 包住子 Run；
- `max_tokens` → 子 Run 的 `max_output_tokens`；
- `max_tool_calls` → 子 Session 上挂一个 observer 统计 `tool.started`，达到上限即 set
  `cancel_event`，走 Runtime 已有的取消路径（孤儿 Tool Call 照常修复）；
- 输入里请求的预算一律与父上限取 `min`，不能靠请求参数放大。

### 5. 子代理默认不能再派生

`capabilities` 未显式指定时，子代理继承父能力集合**减去** `agent.spawn`。
无限扇出必须是显式选择，不是默认行为。

`restrict_registry(registry, capabilities)` 按 `ToolSpec.required_capabilities` 过滤工具集，
子 Run 看不到自己无权使用的工具。

## 后果

- `aicode` 用 `AICODE_SUBAGENTS=true` 启用：只读工作区、无 `process.exec`、`max_depth=1`，
  即「派一个子代理去读代码并汇报」，它自己不能改文件也不能再派人；
- 子代理被审批挂起时，父 Run **不失败**：Tool Result 报告 `state=waiting` 和 `approval_id`，
  父代理可以据此决定下一步；
- Replay 仍是单 Session 边界的：跨父子会话的联合回放需要新的 Bundle 形式，属于后续工作；
- `agents` 因此成为最后一个完成「先有注入点、再有 ADR、最后进公共 API」流程的能力包。
  仍未导出：`plugins`、`mcp`、`evals`、`api`、`cli`。

## 未采纳

- **子 Run 复用父 Session**：会同时破坏单写者不变式和「子代理拥有独立上下文」——
  子 Run 会继承父对话的全部历史。
- **给工具一个通用的事件 sink**：那等于给任意工具往事件日志写任意事件的权限，
  是比本 ADR 想解决的问题大得多的授权缺口。
