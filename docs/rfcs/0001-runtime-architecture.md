# RFC-0001：AIHarness Runtime 与执行面

- 状态：Accepted
- 日期：2026-08-04
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`

## 摘要

AIHarness 采用模块化单体控制面和独立执行面。`runtime/` 只负责状态机、上下文请求、
工具意图和生命周期；`sandbox/`、`plugins/` 和外部 Worker 负责执行有副作用的动作。

## 公共入口

```python
async for event in harness.run(
    session_id=session_id,
    prompt=prompt,
    options=RunOptions(...),
):
    consume(event)
```

运行时只返回 canonical `Event`，CLI、API、TUI、Eval 和远程传输不读取内部 Provider 流。

## 运行不变式

1. `assistant.message` 必须在任何 Tool Call 执行前持久化。
2. Tool Call 必须有唯一配对的 Tool Result；拒绝、取消和恢复也算结果。
3. 任何副作用只能由 `tools/dispatcher.py` 进入 `policy → hooks → sandbox`。
4. Provider Fallback 不得重放不确定是否已产生副作用的工具。
5. 进程崩溃后从事件恢复，不从临时内存猜测状态。
6. Host 执行必须显式 `unsafe=true`，并写入 `run.started/tool.started`。
7. 子代理的能力、预算和工作区只能是父 Run 的子集。
8. Hook 只能消费调用方提供的事件快照和治理证据；有副作用的 Hook 必须经过显式 Trust、Policy
   和 Sandbox，不得自行授予 Approval 或 Capability Lease。
9. MCP 远程工具必须通过 canonical ToolDispatcher；断线恢复不得盲目重放可能已经产生副作用的
   `tools/call`。
10. Plugin 只能在独立的版本化 Plugin Host 子进程中激活；激活前必须重新验证精确 Trust Hash，
    Manifest capabilities/permissions 必须是当前 Run 显式允许集合的子集，Plugin Tool 仍必须
    通过 canonical ToolDispatcher。

## 事件边界

持久事件：消息、工具意图、Policy、Approval、工具结果、Usage、Compaction、Memory、
Subagent 状态和 Run 状态。
临时事件：Token Delta、进度动画和尚未提交的 UI 状态。

事件采用 `schema_version`。未知字段必须向前兼容，未知事件类型由 Reader 保留而非静默丢弃。

## 依赖关系

```text
core ← sessions / models / tools / policy / sandbox / hooks / memory / skills / agents
      ← runtime
runtime ← api / cli / evals / observability
```

具体 Provider、Store、Sandbox 和 Plugin Host 通过 Protocol 注入；Runtime 不做具体类型判断。

## 交付顺序

1. L0 Core + SQLite Event Store + Fake Provider + HostBackend；
2. Runtime/Tool/Policy/CLI 纵向闭环；
3. 真实 Provider、写工具、审批和 DockerBackend；
4. Context/Compaction/Artifacts、Plugins/Skills/Hooks/MCP/Memory；
5. Agents、PostgreSQL、Worker、API、Eval 和 OTel。
