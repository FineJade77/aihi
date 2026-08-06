# ADR-0014：可选 OTel Exporter 与 Golden Task 边界

- 状态：Accepted（M7d-a 扩展）
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0012、ADR-0013

## 决策

1. 提供 `JsonlTelemetrySink` 和可选 `OpenTelemetrySink`。Exporter 在边界再次应用 Redactor，
   JSONL 使用 `allow_nan=false`；OpenTelemetry API 不进入核心必需依赖，未安装时构造失败为稳定
   `exporter_unavailable`。OTel Metric/Cost 必须保留 canonical `unit`，不同 unit 不共用同一个
   instrument，非有限或溢出值 fail closed。
2. `GoldenTask`/`GoldenTaskGrader` 只消费 `ReplayResult`，检查 required/forbidden event types、
   Run state 和 pending Tool Call；不启动 Provider、Tool、Plugin、Hook、Policy 或 Sandbox。它们
   不代表真实 Coding Task 已执行，也不授予任何 Runtime 权限。
3. Exporter 失败由上层 Telemetry facade fail-open；需要重试、backpressure、远程认证或持久化的
   pipeline 必须在后续适配层实现，不改变 Event Store 事实源。
4. `ProviderGoldenTask`/`ProviderGoldenRunner` 是独立的离线 Provider 边界：只消费
   Provider-neutral stream transcript，不执行 Tool、Policy、Hook、Sandbox，不把消息 ID 或工具调用
   ID 当作行为断言；请求只用脱敏 canonical fingerprint 关联。`EvalGate` 输出严格 JSON
   `GateVerdict`，空数据或低于 `min_pass_rate` 时以稳定 `eval_gate_failed` 阻断 CI。

## 原因

将导出和 Golden 评分固定在只读 canonical 边界，可在没有 OTel SDK 或真实 Provider 的本地环境
中进行契约测试，同时避免评估/监控代码意外进入副作用执行链。

## 后续

- 接入真实 OTel SDK、resource/trace propagation、批量导出与限时重试；
- 接入真实 OTel SDK 的 resource/trace propagation、批量导出与限时重试；
- 增加远程 Provider Golden Coding Tasks 和外部评估服务；
- 将成本和安全指标接入统一 CI 合并门禁。
