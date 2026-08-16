# AIHI 评估契约 v1

本文冻结第一版评估边界，定义数据归属、案例格式、发布语义和本地自动化门禁。

## 两套数据集，一个报告契约

`aihi-agent` 与 `aihi-code-agent` 共同纳入评估，但使用不同的 Oracle：

| 数据集 | 归属 | 输入 | 主要 Oracle | 发布规则 |
| --- | --- | --- | --- | --- |
| `aihi-agent-conformance-v1` | `aihi-agent` | 脱敏 `TraceBundle` | Replay、事件和安全不变式 | 必须 100% 通过 |
| `aihi-code-agent-benchmark-v1` | `aihi-code-agent` | 隔离工作区和任务 Prompt | 隐藏测试、回归、范围和安全 | 对比 pass@1 基线 |

一次 Coding Agent 运行可以同时产生产品结果和脱敏 Harness Trace，但两个分数保持独立，不能让一个正确的
代码 Patch 掩盖 Runtime 契约违规。

规范目录为：

```text
evals/
├── schemas/
├── aihi_agent/v1/
└── aihi_code_agent/v1/
```

`evals/schemas/` 中的 JSON Schema 是序列化契约：

- `harness-case.schema.json`：只做 Replay 的通过案例和拒绝案例。
- `code-task.schema.json`：隔离执行的 Coding Agent 任务及其 Oracle。
- `eval-report.schema.json`：稳定的机器可读评估结果。

## Harness 契约案例

案例必须确定性执行，不得调用 Provider、Tool 或 Sandbox。覆盖生命周期顺序、Tool/Result 配对、Approval
挂起与恢复、中断/取消、序号完整性、子 Agent 权限以及脱敏，并同时包含合法 Trace 和带稳定错误码预期的
非法 Trace。

Harness 门禁是二值的：所有必选案例都必须通过。`0.8`、`0.9` 等分数只用于诊断，不能替代发布门槛。

## Coding Agent 基准案例

每个任务固定 Fixture SHA-256、执行限制和 Oracle。默认发布配置为 Docker 且关闭网络。只有以下条件全部满足，
任务才算成功：

```text
隐藏测试
且在要求时通过回归测试
且通过允许/禁止路径检查
且通过 Harness 安全/契约检查
```

v1 Smoke 语料目前包含四个任务，覆盖 Bug 修复、小功能、测试修复和安全边界。提交的脚本化基线只验证
Runner/Oracle 链路，明确不是真实模型能力分数。后续语料切片再加入重构、仓库理解、指令遵循、中断/恢复
和 Subagent 使用。

## 可复现性与兼容性

- 数据集一旦作为基线使用即不可变。
- 不为适配实现而重写 Fixture 或隐藏 Oracle。
- 报告必须是严格 JSON；真实模型运行时记录 Provider/Model 以及 Prompt/Tool Hash。
- 凭据和未脱敏的模型/Tool 输出不能进入数据集或提交的报告。
- Schema 或语义发生变化时创建新版本，不能静默改变旧案例。

## 执行模式

- `offline`：只做 Replay 的 Harness 契约评估，不进行外部调用。
- `pr`：Harness 全集加少量确定性的 Coding Agent Smoke Case。
- `nightly`：完整基准、多次重复和基线对比。
- `release`：与 nightly 相同，并应用发布门槛。

本地门禁命令：

```bash
python3 -m scripts.evals.run --mode offline
python3 -m scripts.evals.run --mode pr
```

报告写入 `eval-results/<mode>/`。退出码 `0` 表示门禁通过，`1` 表示评估案例失败，`2` 表示准备或配置失败。
`nightly` 和 `release` 必须显式传入 `--config <path>`，该配置必须使用 Docker 且关闭网络；缺少配置时会 fail
closed。`.github/workflows/evals.yml` 会在 Pull Request 上运行 `pr`，其他模式通过显式 workflow dispatch 触发，
不会在仓库中保存凭据。
