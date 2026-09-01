# `aihi_code_agent` Cache/Compaction 评估 v1

这个确定性的 PR/Release 门禁对同一个长 Session 任务执行两次：一次提供足够容量以保留原始历史，另一次
将输入置于滚动 Compaction 水位。两次都必须通过工作区与 Harness Oracle。联合对比还要求关键状态召回率为
100%、Cache Family Hash 相同、任务内 Cache Key 不漂移、观察到 Cache Hit、至少发生一次 ContextState v2
滚动 Compaction，并且压缩后的 Provider 输入 Token 更少。

历史输入由一次 User 请求和后续 60 个闭合 Tool Call/Result Exchange 构成，共 121 条消息，专门防止把完整
Coding Run 误判成一个无法压缩的 User Turn。

Evaluator 使用打包的 Coding Agent Prompt、Provider-neutral Runtime 和脚本化 Fake Provider。它验证确定性的
Cache/Compaction/Report 链路，不是真实模型能力或延迟基线。耗时只用于诊断，不作为回归门禁。
