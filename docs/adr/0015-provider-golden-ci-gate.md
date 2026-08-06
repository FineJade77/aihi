# ADR-0015：Provider Golden Task 与离线 CI 门禁

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0013、ADR-0014

## 决策

1. Provider Golden fixture 只保存 Provider-neutral stream chunks、模型名、Provider 名和脱敏请求
   fingerprint；不得保存 API Key、完整请求正文或带临时 ID 的断言。
2. `ProviderGoldenRunner` 接收显式注入的 Provider，顺序消费 `Provider.stream`，规范化
   `MessageStart`、Block、Delta 和 `MessageEnd`；Provider 异常转换为稳定 error code，不能自动重试。
3. Provider Golden 结果与既有 Replay 结果都可交给 `EvalGate`。门禁必须拒绝空数据，按
   `min_pass_rate` 判断，并输出不含原始模型/工具内容的严格 JSON `GateVerdict`。
4. Golden 运行不进入 Runtime 的副作用链，不拥有 Tool、Policy、Hook、Sandbox 或网络授权；真实
   Provider 和远程 Telemetry pipeline 由后续适配层显式提供。

## 原因

将 Provider 兼容性回归固定在可重复、无网络的契约边界，避免把供应商 SDK、凭据或随机生成的调用
标识写入仓库，同时让 CI 可以用稳定的机器可读结果阻止退化。

## 后续

- 接入需要凭据和隔离网络的远程 Provider 任务，并单独配置重试和成本预算；
- 将 GateVerdict 与真实 OTel pipeline、构建系统和 PR 状态检查集成。
