# ADR-0021：流式增量不落盘与批量事件提交

状态：Accepted
日期：2026-08-06
关联：ADR-0002（Event Store 与快照）、RFC-0001、ARCHITECTURE §4

## 背景

`Event.ephemeral` 字段从 M1 起就存在，ARCHITECTURE §4 和 AGENTS.md 也都写明「流式 Token Delta
可作为临时事件，不应按每个 Token 写入存储」。但没有任何代码路径实现它：`Session.append` 不检查
该字段，`Event.persisted()` 反而主动把它清成 `False`。

结果是 `RunCoordinator._consume_provider` 把**每一个** stream chunk 作为持久事件写入 Store。
实测一次只输出 2000 字符纯文本的 run：

```
总事件 178，其中 model.chunk 171 个，全部落盘
SQLite 单条 append ≈ 0.6 ms → 仅 chunk 就占约 102 ms
```

这些写发生在 run 的 async 循环里，且 `Session._notify_observers` 会对每个事件每个 observer 做一次
`deepcopy`，接上 Telemetry 后放大系数相同。全仓库没有任何消费者读取 `model.chunk`。

## 决策

### 1. Ephemeral 事件真正实现

- 新增 `Session.emit(event)`：只通知 observer，不写 Store，不推进 `head_seq`；要求
  `ephemeral=True`，否则拒绝。
- `Session.append`/`append_many` 拒绝 `ephemeral=True` 的事件。两条路径互斥且 fail closed，
  不会出现「本该落盘的事件被静默丢弃」或「本该临时的事件被静默持久化」。
- `model.chunk` 改为 `ephemeral=True` 并通过 `emit` 发布。
- `Telemetry.record_event` 忽略 ephemeral 事件：有界 sink 会被 token delta 挤掉真实记录。

Assistant 消息本身仍然持久化，因此同一次流式输出的**结果**始终可恢复、可 Replay；被丢弃的只是
中间增量。

### 2. 无副作用的相邻事件合并为一个事务

以下位置改用 `append_many`：

- run 开始：`user.message` + `run.started`/`run.resumed`；
- run 结束：最终 `run.state_changed` + `run.completed`/`run.failed`/`run.interrupted`；
- 上下文记账：`artifact.created` × N + `compaction.created`。

**不合并**跨越副作用边界的事件。`tool.requested`、`policy.decided`、`tool.started` 必须在工具执行
前各自落盘，`tool.completed` 和 Tool Result 必须在执行后立即落盘 —— 这是崩溃恢复能判断「工具是否
可能已经产生副作用」的唯一依据，合并会直接破坏该保证。

## 后果

实测同一个 run：

| | 之前 | 之后 |
|---|---|---|
| 持久事件 | 178 | 7 |
| 耗时 | 158.8 ms | 20.8 ms |
| 送达 observer 的增量 | 171 | 171（不变） |

- 事件日志现在只包含事实，Replay 和 Eval 的输入体积下降一个数量级；
- 需要实时增量的 UI 必须在进程内 `session.add_event_observer` 订阅；控制面
  `/sessions/{id}/events` 不再返回 chunk，如需远程流式必须另建流式端点；
- `Session.message_event()` 从 `add_message()` 中拆出，供调用方自行批量提交；
- 单条 `append` 的成本没有改变，改变的是次数。这也是为什么不需要把 Store 改成 async：
  真正的问题是写放大，不是阻塞。

## 未采纳

- **把 EventStore Protocol 改成 async**：会传染到 Session、Runtime、Memory、Skill、Plugin、CLI 与
  API 的全部调用点，并迫使 FastAPI 路由从同步改为 `async def`，反而把 SQLite 写堵在唯一的事件
  循环上。当前单 run 场景下事件循环没有其他待办任务，async 化没有可兑现的收益。
- **把 chunk 批量落盘**：仍然是在存储事实源之外的数据。要重放模型输出应使用 Provider Golden
  fixture，不是事件日志。
