# `aihi_agent` 一致性语料 v1

本版本在 `manifest.jsonl` 中包含合法和故意非法的脱敏 TraceBundle 案例，覆盖完成/失败、Approval 挂起与
恢复、带待处理 Tool 的中断、取消、脱敏、序列/身份完整性和终态 Payload 完整性。案例可使用
`HarnessConformanceRunner` 离线评估。后续只新增案例，不改变这些冻结条目的含义。

加法新增的 `cache-compaction-v2` Golden Trace 覆盖 Cache Read/Write Usage、稳定 Cache Family 身份、
压力/Target Metadata 和 Schema v2 `compaction.created` Record，不改变旧条目。
