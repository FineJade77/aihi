# AIHI 评估契约 v1

本文冻结第一版评估边界，定义数据归属、案例格式、发布语义和本地自动化门禁。

## 三套数据集，一个报告契约

`aihi-agent` 与 `aihi-code-agent` 共同纳入评估，但使用不同的 Oracle：

| 数据集 | 归属 | 输入 | 主要 Oracle | 发布规则 |
| --- | --- | --- | --- | --- |
| `aihi-agent-conformance-v1` | `aihi-agent` | 脱敏 `TraceBundle` | Replay、事件和安全不变式 | 必须 100% 通过 |
| `aihi-code-agent-benchmark-v1` | `aihi-code-agent` | 隔离工作区和任务 Prompt | 隐藏测试、回归、范围和安全 | 对比 pass@1 基线 |
| `aihi-code-agent-context-v1` | `aihi-code-agent` | 成对的确定性长 Session | Cache/Compaction 不变式与任务结果 | 必须 100% 通过 |

一次 Coding Agent 运行可以同时产生产品结果和脱敏 Harness Trace，但两个分数保持独立，不能让一个正确的
代码 Patch 掩盖 Runtime 契约违规。

规范目录为：

```text
evals/
├── schemas/
├── aihi_agent/v1/
├── aihi_code_agent/v1/
└── aihi_code_agent/context-v1/
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

v1 Smoke-plus 语料目前包含九个任务，覆盖 Bug 修复、小功能、测试修复、安全边界、重构、仓库理解、
指令遵循、中断/恢复和 Subagent 使用。提交的脚本化基线只验证 Runner/Oracle 链路，明确不是真实模型能力
分数。

## Cache/Compaction 联合评估

`aihi-code-agent-context-v1` 使用打包 Prompt，对同一任务分别运行未压缩长 Session 基线和
ContextState v2 Hard Compaction Profile。两次工作区结果及导出的 Harness Trace 都必须通过。联合门禁
还要求关键状态召回率为 100%、Cache Family Hash 相同、任务内 Cache Key 变化为 0、至少一次 Hard
Compaction、观察到 Cache Hit，并且压缩后输入 Token 更少。任务耗时会记录，但不作为墙钟回归门禁。

Harness 语料还包含只做 Replay 的 `cache-compaction-v2` Golden Trace，覆盖 Cache Read/Write Usage、
稳定前缀身份、压力 Metadata 和 Schema v2 Compaction Record。这样无需改变既有 Coding Benchmark 与
已审核真实基线，也能让联合行为成为 PR 和 Release 的必选预检。

## 已审核真实基线

首份审核后的真实结果于 2026-08-17 使用 DeepSeek `deepseek-v4-flash` 采集。9 个基础任务各重复 3 次，
27 次尝试通过 26 次：经验 pass@1 为 96.3%，至少一次成功率为 100%，稳定通过率为 88.9%，任务耗时
P50 为 16.0 秒、P95 为 59.7 秒。唯一失败尝试在 90 秒任务上限被中断；该基础任务另外两次尝试均通过。
不含凭据且带 Prompt/Tool Hash 的基线保存在
[`evals/aihi_code_agent/v1/baselines/deepseek-v4-flash-2026-08-17.json`](../evals/aihi_code_agent/v1/baselines/deepseek-v4-flash-2026-08-17.json)。
该结果用于项目回归对比，不代表对模型通用能力的断言。

PR 模式只使用脚本化参考基线验证 Runner/Oracle 链路。Nightly/Release 会选择 Provider 与 Model 都和
真实报告匹配的审核基线；经验 pass@1 低于该基线时，Live 门禁失败。没有审核基线的 Profile 会显示
`baseline unavailable`，不会回退到脚本化分数，并在生成待审核 Artifact 的同时保持所有尝试必须通过的
严格门禁。单 Profile 对比可用显式 `--baseline` 覆盖自动选择。

## 可复现性与兼容性

- 数据集一旦作为基线使用即不可变。
- 不为适配实现而重写 Fixture 或隐藏 Oracle。
- 报告必须是严格 JSON；真实模型运行时记录 Provider/Model 以及 Prompt/Tool Hash。`prompt_sha256`
  对打包的 Coding Prompt 模板取指纹，`tools_sha256` 对运行时实际组装并暴露给模型的 Tool 定义取指纹。
- Trace 脱敏后仍保留数值型 Cache/Token/Compaction 指标；`access_token` 等凭据字段仍会脱敏。完整 Cache
  Key 和 Prompt 不进入报告。
- 凭据和未脱敏的模型/Tool 输出不能进入数据集或提交的报告。
- Schema 或语义发生变化时创建新版本，不能静默改变旧案例。

## 执行模式

- `offline`：只做 Replay 的 Harness 契约评估，不进行外部调用。
- `pr`：Harness 全集、确定性的 Cache/Compaction 联合对比和 Coding Agent Smoke Case。
- `nightly`：完整基准、多次重复和基线对比。
- `release`：与 nightly 相同，并应用发布门槛。

本地门禁命令：

```bash
python3 -m scripts.evals.run --mode offline
python3 -m scripts.evals.run --mode pr
python3 -m scripts.evals.run --mode nightly \
  --config /secure/model-a.toml \
  --config /secure/model-b.toml \
  --repeat 3 \
  --output eval-results/nightly
```

报告写入 `eval-results/<mode>/`。退出码 `0` 表示门禁通过，`1` 表示评估案例失败，`2` 表示准备或配置失败。
`nightly` 和 `release` 必须显式传入 `--config <path>`，配置必须指定真实 Provider、`api_key_env`、Docker
镜像、`permission_mode = "bypass"` 并关闭网络；Fake Provider、MCP Server、缺少凭据、占位 Model 或交互式
Permission Mode 都会 fail closed。提交的
`evals/aihi_code_agent/v1/nightly.config.example.toml` 是不含凭据的模板。`nightly` 和 `release`
默认每个任务执行三次，可用 `--repeat` 覆盖。重复传入 `--config` 可以在同一次运行中
比较多个 Provider/Model；所有配置必须先通过 fail-closed 校验，之后才会产生真实 Provider 调用。

每个真实案例记录任务耗时、模型调用数、Tool 调用数、输入/输出/Cache Read/Cache Write Token、Cache Hit
Ratio、Cache Key 变化、Soft/Hard Compaction 次数，以及 Provider 能提供时的成本。
汇总报告提供经验 `pass_at_1`（各任务成功比例的均值）、至少一次成功率、所有尝试稳定通过率和任务耗时
P50/P95。所有非 Offline 门禁还会写入 `context.json` 与 `context-comparison.json`。单个 Live Profile 写入
`code.json` 与 `baseline-comparison.json`；多 Profile 在 `profiles/` 下分别写入 Live 报告，并额外生成
不含凭据的 `live-summary.json`。脚本化基线始终只用于诊断 Runner/Oracle 链路，不作为模型分数。
审核后的真实基线按 Provider/Model 精确匹配并用于 pass@1 回归门禁；无基线 Profile 不会静默使用脚本基线。

`.github/workflows/evals.yml` 在 Pull Request 上运行确定性的 `pr` 模式。手工触发真实评测时，它从
`AIHI_CODE_EVAL_CONFIGS_B64` Repository Secret 重建权限为 0600 的 TOML，并从对应 API Key Secret 读取
凭据。该 Secret 每个非空行保存一个 TOML 的 Base64，可在一次 Dispatch 中比较多个模型。独立的
`.github/workflows/ci.yml` 会执行 Python 3.11/3.12 编译、Ruff、严格 Mypy、完整测试/打包测试、
TypeScript 类型检查以及 CLI 构建和测试。

生成 Secret Payload 时不要把 API Key 值写进 TOML：

```bash
base64 < /secure/model-a.toml | tr -d '\n'
base64 < /secure/model-b.toml | tr -d '\n'
```

将两行结果保存为 `AIHI_CODE_EVAL_CONFIGS_B64`；真实 Key 分别保存到 `OPENAI_API_KEY`、
`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY` 或 `AIHI_CODE_AGENT_API_KEY` GitHub Secret。
