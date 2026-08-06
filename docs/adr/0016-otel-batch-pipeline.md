# ADR-0016：有界 OTel 批量 Pipeline 与 OTLP/HTTP 出口

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0012、ADR-0014

## 决策

1. Runtime 继续只依赖 `TelemetrySink`；`OtelBatchPipeline` 在边界完成再次脱敏、有界队列、显式
   背压、批量发送和有限重试。队列满时必须明确选择抛错、丢新记录或丢旧记录，不能隐式阻塞或无限
   增长。
2. 只对 transport 明确标记 `retryable=true` 的错误做指数退避；重试耗尽后丢弃当前遥测批次并返回
   稳定 `otel_export_retry_exhausted`，不重放 Runtime 副作用，也不修改 Event Store。
3. Resource 统一由 `OTelResource` 管理并在构造和发送边界脱敏；Bearer token 只生成 Authorization
   header。`W3CTracePropagator` 仅接受严格 W3C `traceparent`，零 ID、非法版本/flags 和异常长度
   必须拒绝。
4. `OtlpHttpTransport` 使用注入或显式配置的 HTTP client，输出 resource/spans/metrics/logs 的
   OTLP/HTTP JSON envelope；HTTP 429/5xx 可重试，其他 4xx 不重试，异常详情不得包含 response body
   或认证信息。Runtime 不因安装 OTel extra 而自动开启远程出口。

## 原因

把队列、认证、重试和网络放在可替换的 pipeline 适配层，既能在无网络测试环境验证 OTel 语义，又能
避免观测故障改变 Runtime 的持久化结果；显式背压和丢弃统计使运营侧可以看见数据完整性损失。

## 后续

- 根据部署需要接入 OTel SDK BatchSpanProcessor、mTLS/secret broker 和持久化 spool；
- 增加跨 Worker trace refresh 与 OTLP exporter 的端到端合规测试。
