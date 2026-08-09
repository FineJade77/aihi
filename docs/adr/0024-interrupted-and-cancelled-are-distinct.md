# ADR-0024：区分 INTERRUPTED 与 CANCELLED，并补上放弃 Run 的出口

状态：Accepted
日期：2026-08-07
关联：ADR-0020（Approval 挂起态）、ARCHITECTURE §4.1

## 背景

取消一个 Run 时，Runtime 把状态置为 `CANCELLED`，却追加名为 `run.interrupted` 的事件；
ReplayEngine 又把 `run.interrupted` 映射回 `CANCELLED`。事件是事实源，而事实源和状态机
用了两个不同的词描述同一件事：`ReplayResult.run_states` 显示 `cancelled`，
`event_type_counts` 显示 `run.interrupted`，任何消费者都要额外知道这层翻译。

ARCHITECTURE 早期版本的状态图里同时有 `INTERRUPTED` 和 `CANCELLED`，并写明
「Interrupted 只能显式 Resume」，但实现里从来只有一个状态。`agents/` 反而是对的：
`AgentState.INTERRUPTED` 与 `AgentState.CANCELLED` 一直是两回事
（`graph.interrupt()` vs `graph.cancel()`）。

ADR-0020 引入 `WAITING_APPROVAL` 之后还暴露出第二个问题：**挂起的 Run 没有终止出口**。
可以批准、可以拒绝、可以恢复，但没有「算了，不做了」——挂起的 Tool Call 会永远悬着。

## 决策

### 1. 两个终态，各自对应一个事件

| 状态 | 事件 | 含义 | 可恢复 |
|---|---|---|---|
| `INTERRUPTED` | `run.interrupted` | Run 在执行中被打断（`cancel_event`、Ctrl-C） | 是 |
| `CANCELLED` | `run.cancelled` | 所有者显式放弃这个 Run | 否 |

`asyncio.CancelledError` 现在映射到 `INTERRUPTED`，与它一直在写的事件名一致；
`RunResult.error` 相应变为 `run_interrupted`。ReplayEngine 分别映射两个事件，不再翻译。

这也与 `agents/` 的既有语义对齐：整个代码库现在用同一组词。

### 2. `RunCoordinator.abandon(session, run_id, reason)`

放弃一个**当前没有在执行**的 Run：

- 合成挂起 Tool Call 的错误结果，日志里不留悬空调用；
- 追加 `run.state_changed(cancelled)` + `run.cancelled`（同一事务）；
- 该 Run 不再出现在 `suspended_runs()` 中。

未知 Run 或已终止的 Run 会被拒绝，不会写出第二个终态事件。

`abandon` 是同步方法：它不执行任何工具，只关闭事件流。

## 后果

- 应用层的产品闭环补齐为 `approve` / `--deny` / `resume` / `abandon`；
- `run.cancelled` 是新增事件类型，消费者需要识别；`run.interrupted` 语义不变，
  只是它现在真的对应 `INTERRUPTED` 状态；
- `RunResult.error` 从 `run_cancelled` 变为 `run_interrupted`（打断路径）。
  依赖该字符串的调用方需要更新 —— 属于公共行为变更，故有本 ADR。

## 未采纳

- **把事件改名为 `run.cancelled` 并只保留一个状态**：会丢掉「打断可恢复 / 放弃不可恢复」
  这个对终端 Agent 有实际意义的区分，而这个区分 `agents/` 已经在用。
- **让 `abandon` 变成 async 并去取消在飞任务**：正在执行的 Run 应该通过
  `cancel_event` 打断（→ `INTERRUPTED`）。放弃针对的是已经停下来的 Run，
  把两件事合并会让「谁在写这个 Session」重新变得不清楚。
