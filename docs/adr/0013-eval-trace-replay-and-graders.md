# ADR-0013：脱敏轨迹回放与确定性评估

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0002、ADR-0012

## 决策

1. Eval 输入使用显式 `redacted=true` 的单 Session `TraceBundle`。构造和加载时对事件递归
   Redactor 规范化、深度冻结嵌套对象，并对完整规范化 JSON 计算 SHA-256；Hash、Schema、Session、
   Event ID、连续 `seq` 或 `ephemeral` 校验失败时拒绝回放。自定义 Redactor 的输出仍必须经过
   Harness canonical Redactor，不能放宽安全上限。
2. `ReplayEngine` 只重建 Run 状态、Tool 生命周期、Message/Tool Result 配对和状态摘要 Hash；不
   调用 Provider、Tool、Plugin、Hook、Policy 或 Sandbox。Policy 拒绝的 Tool 仍必须允许其后持久化
   的 Tool Result，工具 started/completed 则必须属于同一 Run；终态后的事件和重复终态拒绝。
3. `EvalDataset` 使用严格 JSONL，Case ID 唯一，expected/metadata 和 Grade details 禁止 NaN/Inf
   或不可序列化对象。`EvalRunner` 只将 `ReplayResult` 交给 `Grader`；内置 Grader 的 score 必须
   是有限 `[0,1]` 数值，Composite 以确定性平均值计算。

## 原因

离线评估必须可重复且不能重新产生副作用。完整事件 Hash 和严格状态不变式让损坏或被篡改的
轨迹 fail closed；把 Grader 与 Runtime 解耦，便于后续接入 Golden Task、Provider Contract 和
安全回归集。

## 后续

- 增加脱敏轨迹导出文件格式版本迁移和大数据集流式读取；
- 接入 Fake/Replay Provider、Golden Coding Tasks、安全评分器和 OTel trace 关联；
- 在 CI 中将 Replay 状态 Hash、策略拒绝率和工具安全回归作为合并门禁。
