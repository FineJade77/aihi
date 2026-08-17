# `aihi_code_agent` 基准 v1

本版本包含九个确定性的 Smoke-plus Task，覆盖 Bug 修复、功能实现、测试修复、安全边界、重构、仓库理解、
指令遵循、中断/恢复和 Subagent 规划。Manifest 固定了 Fixture Hash 与类似隐藏测试的 Oracle 命令，
`baseline.json` 记录脚本化参考执行器结果。该基线用于验证 Runner/Oracle 链路，明确不是真实模型能力分数。

首份审核后的真实 Provider 基线见
[`baselines/deepseek-v4-flash-2026-08-17.json`](baselines/deepseek-v4-flash-2026-08-17.json)。
DeepSeek `deepseek-v4-flash` 在 9 个任务、每个重复 3 次的 27 次尝试中通过 26 次：经验 pass@1 为
96.3%，所有基础任务都至少成功一次，稳定通过率为 88.9%，任务耗时 P50 为 16.0 秒、P95 为 59.7 秒。
唯一失败尝试达到 90 秒任务上限，并被持久化为 `INTERRUPTED`；审核后分类为 `execution_timeout`。

`nightly.config.example.toml` 说明真实 Provider、凭据环境变量和 Docker/禁网配置要求；它只是模板，必须在仓库
外提供 Model 和环境变量后才能使用。
v1 每个任务的 live Provider 端到端时限为 90 秒，覆盖模型与工具往返以及最终 Oracle 检查前的任务执行。

在仓库根目录运行重复的多模型基准：

```bash
python3 -m scripts.evals.run --mode nightly \
  --config /secure/model-a.toml \
  --config /secure/model-b.toml \
  --repeat 3 \
  --output eval-results/nightly
```

`live-summary.json` 对比经验 pass@1、稳定通过率、任务耗时 P50/P95、Token、模型/Tool 调用数以及 Provider
能提供时的成本。报告不包含配置路径、凭据和原始模型/Tool 输出。真实报告默认只保留在本地或 CI Artifact；
公开报告前仍需检查模型名称、指标和脱敏边界。

PR 模式只与脚本化参考基线比较。Nightly/Release 会自动选择 Provider 和 Model 均匹配的审核基线，并在
经验 pass@1 退化时失败。尚无审核基线的新模型会显示 `baseline unavailable`，同时要求所有尝试通过；随后
可单独审核并版本化其生成报告。单 Profile 运行需要覆盖默认选择时可显式传入 `--baseline`。
