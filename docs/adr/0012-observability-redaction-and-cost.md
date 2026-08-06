# ADR-0012：旁路可观测性、脱敏与成本核算

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0002、ADR-0011

## 决策

1. Core/Runtime 不直接依赖 OpenTelemetry。`TraceContext`、`Observation`、`MetricPoint`、
   `CostRecord` 和 `TelemetrySink` 是 vendor-neutral canonical contract，未来 OTel adapter
   只能映射这些字段，不能把 provider SDK 类型带入 Core。
2. `Session` 允许注册同步 Event observer。observer 只接收已成功追加事件的深拷贝，异常被吞掉，
   不得改变事件、Policy、Tool 或 Sandbox 结果。M7a 的 `Telemetry` facade 将事件、指标和成本
   记录发送到有界 Sink；自定义 Sink 必须非阻塞，否则应在适配层异步化/限时。
3. Redactor 在进入 Sink 前执行：敏感键直接替换，常见 Bearer/API token 模式替换，非有限数字、
   超长字符串、深层/超量容器和未知对象 fail-closed。不得把完整模型/工具输出或 credential 写入
   telemetry；需要关联时使用脱敏 payload hash。
4. CostRecord 只接受非负 Token、非负有限价格和非负有限 Usage cost；计算结果必须有限。输入、
   输出和缓存 Token 与每千 Token 价格在同一个 canonical 记录中，避免不同 Provider 的费用字段
   泄漏到 Runtime。

## 原因

观测系统本身不能成为新的 Secret 泄露面，也不能因 exporter 故障阻塞副作用链路。先固定脱敏、
数值和 trace 语义，后续接入 OTel、结构化日志或远程指标系统时仍可重放并比较同一事件轨迹。

## 后续

- 增加有界异步 Sink、export retry/backpressure 和 OTel trace/metrics adapter；
- 为外部 Worker refresh、Run/Tool span 和模型 Usage 自动生成更细粒度的 trace；
- 在 Eval/Replay 中导出脱敏轨迹并验证成本、延迟、压缩率和策略拒绝率。
