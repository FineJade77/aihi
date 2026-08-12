# ADR-0035：事件驱动的 Coding CLI Transcript

- 状态：Accepted
- 日期：2026-08-12
- 关联：[ADR-0034](0034-code-protocol-0.2-and-replay.md)

## 背景

TUI 已能完整 replay Session，但只从历史中找最后一条 `assistant.message`，实时路径又单独维护
`answerText`、stream chunk 和若干 Tool/Approval 面板。这造成两个问题：恢复后看不到用户问题和
Tool 执行过程；Replay 与实时通知使用不同逻辑，增加显示状态漂移的风险。

## 决策

在 `aihi-code-cli` 应用层建立纯 Transcript projector：

- `user.message`、`assistant.message`、Tool 生命周期、Approval 与失败 Run 投影为稳定展示项；
- Replay Event 与实时 durable notification 使用同一个 reducer，以 Session seq 去重并检查连续性；
- Replay 期间到达的通知按 Session 暂存，Replay 完成后按 seq 排序合并；发现缺口时重新完整 replay；
- Tool Call、Tool 生命周期、Approval 与 Tool Result 通过 `tool_call_id` / `approval_id` 原位更新，
  不为一次 Tool 调用追加多行互相矛盾的状态；
- `model.chunk` 只进入临时 stream buffer，canonical `assistant.message` 到达后清空，不持久化为
  Transcript 项；
- Tool preview 只显示命令、路径、pattern、query、objective 或 name 等白名单字段，禁止通用
  `JSON.stringify(input)`；白名单文本内的 credential pattern 仍须脱敏，避免把凭据带入终端历史。

Transcript 是可丢弃投影，不新增 Worker command、不改变 Event Schema，也不成为事实源。

## 后果

- 重连后的界面与实时运行展示同一条用户/助手/Tool 时间线，并可恢复正在执行的 Run ID；
- 当前视图只渲染最近 12 个展示项，Transcript 投影仍由完整 replay 构建，事实保留在 Event Store；
- 若未来需要完整滚动、搜索或折叠，应在该投影之上实现 viewport，不得创建第二套消息状态模型。
