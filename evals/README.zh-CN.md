# AIHI 评估数据

本目录保存带版本的评估规范和数据集，不会被打包进任何 Python wheel，也不是运行时包的一部分。

两套数据集的归属不同：

- `aihi_agent/` 保存 Provider-neutral Harness 契约案例，由脱敏事件 Trace 的离线 Replay 评估。
- `aihi_code_agent/` 保存 Coding Agent 任务，在隔离工作区执行，并使用隐藏测试、修改范围检查和导出的
  Harness Trace 评分。

Schema 位于 `schemas/`。完整契约见
[`docs/EVALUATION.zh-CN.md`](../docs/EVALUATION.zh-CN.md) 和
[`docs/EVALUATION.md`](../docs/EVALUATION.md)。

数据集目录使用 `v1`、`v2` 等版本。如果 Fixture 或 Schema 改变了既有案例的含义，必须创建新的数据集
版本；不要为了让实现通过而改写冻结案例。
