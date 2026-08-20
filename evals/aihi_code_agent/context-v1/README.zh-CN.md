# `aihi_code_agent` Cache/Compaction 评估 v1

这个确定性的 PR/Release 门禁对同一个长 Session 任务执行两次：一次提供足够容量以保留原始历史，另一次
将输入置于 Hard Compaction 阈值。两次都必须通过工作区与 Harness Oracle。联合对比还要求关键状态召回率
为 100%、Cache Family Hash 相同、任务内 Cache Key 不漂移、观察到 Cache Hit、至少发生一次 ContextState
v2 Hard Compaction，并且压缩后的 Provider 输入 Token 更少。

Evaluator 使用打包的 Coding Agent Prompt、Provider-neutral Runtime 和脚本化 Fake Provider。它验证确定性的
Cache/Compaction/Report 链路，不是真实模型能力或延迟基线。耗时只用于诊断，不作为回归门禁。
