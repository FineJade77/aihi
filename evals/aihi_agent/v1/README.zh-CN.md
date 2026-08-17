# `aihi_agent` 一致性语料 v1

本版本在 `manifest.jsonl` 中包含合法和故意非法的脱敏 TraceBundle 案例，覆盖完成/失败、Approval 挂起与
恢复、带待处理 Tool 的中断、取消、脱敏、序列/身份完整性和终态 Payload 完整性。案例可使用
`HarnessConformanceRunner` 离线评估。后续只新增案例，不改变这些冻结条目的含义。
