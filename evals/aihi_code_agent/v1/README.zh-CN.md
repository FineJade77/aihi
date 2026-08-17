# `aihi_code_agent` 基准 v1

本版本包含九个确定性的 Smoke-plus Task，覆盖 Bug 修复、功能实现、测试修复、安全边界、重构、仓库理解、
指令遵循、中断/恢复和 Subagent 规划。Manifest 固定了 Fixture Hash 与类似隐藏测试的 Oracle 命令，
`baseline.json` 记录脚本化参考执行器结果。该基线用于验证 Runner/Oracle 链路，明确不是真实模型能力分数。
`nightly.config.example.toml` 说明真实 Provider、凭据环境变量和 Docker/禁网配置要求；它只是模板，必须在仓库
外提供 Model 和环境变量后才能使用。
