# ADR-0027：父子会话联合 Replay

状态：Accepted
日期：2026-08-07
关联：ADR-0023（Subagent 执行）、ADR-0021

## 背景

子代理跑在独立 Session 中，以保住「一个 Session 一个写者」的不变式（ADR-0023）。代价是
单个日志都讲不完整个故事：父会话只有一条 `task` 工具结果，子会话不知道自己为谁工作。
`TraceBundle` 按构造是单会话的 —— 它的 SHA-256 和序列号校验正因此才有意义。

## 决策

### 1. 组合 Bundle，而不是放宽 Bundle

`TraceGraph(root, children)` 持有多个 `TraceBundle`。每个 Bundle 保留自己的单会话哈希与
序列校验，Graph 只增加会话之间的链接。不给 `TraceBundle` 增加多会话模式。

### 2. 链接是校验出来的，不是假定的

构造 Graph 时逐条验证并 fail closed：

- 会话 ID 不重复；
- 每个子会话恰好一条 `subagent.started` 和一条 `subagent.completed`，且 `task_id` 一致；
- `parent_session_id` 必须是本 Graph 的 root；
- `parent_run_id` 必须是 root 中真实存在的 Run；
- 同一 `task_id` 不能出现在多个子会话；
- `subagent.completed` 必须带 result。

缺结果的委派、指向图外父会话的子会话、重复任务，都会被拒绝，而不是当作完整链路回放。

`replay_graph()` 逐会话调用现有 `ReplayEngine`，再输出 `Delegation` 结构与整体
`state_sha256`。Replay 仍然只做状态投影，不执行任何副作用。

### 3. 修正：subagent 记录是会话级的

ADR-0023 让 `subagent.started` / `subagent.completed` 带 `run_id=child_run_id`。但
`subagent.completed` 写在子 Run 的终态事件**之后**，而 ReplayEngine 正确地拒绝任何发生在
终态之后的事件 —— 于是**子会话根本无法单独回放**。这个缺陷直到本次真正去回放子会话才暴露。

两条记录描述的是「关于某个子 Run 的事实」，而不是该 Run 的步骤。因此它们改为会话级
（`run_id=None`），子 Run 的 id 放进 payload 的 `child_run_id`。

这是对同日 ADR-0023 的修正，没有已发布消费者，故不升 `EVENT_SCHEMA_VERSION`；
冻结语料已同步更新。

## 后果

- 一次委派现在可以端到端审计：父 Run 状态、子 Run 状态、任务归属与结果状态在一次回放中给出；
- `TraceGraph` 只表达一层父子。多层嵌套需要递归结构，等有真实需求再做；
- 已知局限：兼容性语料守住「类型覆盖」和「读取端不漂移」，但不校验 fixture 是否仍与**写入端**
  一致 —— 本次 payload 变更就是人工同步的。要闭合这一环，需要从真实运行生成语料。
