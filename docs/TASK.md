# AIHarness 实施任务

状态：基线确认，按依赖顺序实施
架构基线：[ARCHITECTURE.md](ARCHITECTURE.md)

## 交付原则

- 每个里程碑必须有可运行的纵向链路；
- 所有新能力先定义事件和可测试协议，再接入实现；
- 安全边界在第一条工具链路中建立，不延后到产品层；
- 原始事件不可覆盖，压缩、Memory 和 Eval 都只能追加派生数据；
- Host 是本地首选，但没有显式 `unsafe=true` 时任何工具都不得执行。

## M0：工程基线与文档

### 任务

- 建立 `pyproject.toml`、`src/aiharness`、`tests`、`docs/rfcs`、`docs/adr`、`plugins`、`examples`。
- 固定 Python 3.11+、代码格式、静态检查、测试命令。
- 建立公共错误码、事件 Schema Version 和兼容性策略。
- 建立依赖方向检查：`core` 不得反向依赖业务包，Provider 具体实现不得泄漏到 Runtime。

### 验收

- `python -m compileall src` 成功；
- `pytest`、`ruff`、`mypy` 命令可执行；
- 事件 JSON 可以写入并读回；
- 文档目录和 ADR/RFC 状态一致。

## M1：L0 核心与事件化会话

### 任务

- `core/`：Canonical Message、ContentBlock、ModelRequest/Response、Usage、ToolSpec、Event、ID、错误。
- `sessions/`：SQLite WAL Event Store、InMemory Store、Session Aggregate、Projection、Snapshot 接口。
- 使用 `expected_seq` 实现并发冲突检测。
- 实现 `session.created`、`user.message`、`assistant.message`、`tool.result` 等基础事件。
- 实现 Session Load、List 和进程重启后的完整恢复。

### 验收

- 任意 Message/Event 可 JSON 无损往返；
- 两个并发追加只有一个成功，另一个得到稳定冲突错误；
- 进程杀死后不会丢失已提交事件；
- 从事件重放得到的消息与实时 Projection 一致；
- 任何压缩实现都不能覆盖原始事件。

## M2：Runtime、Fake Provider 与最小工具闭环

### 任务

- `runtime/`：可恢复 Agent State Machine、Run Coordinator、取消与恢复。
- `models/`：Provider Protocol、Capabilities、7 种 Stream Chunk、Fake Provider。
- `tools/`：Tool Protocol、Schema 校验、确定性 Registry、Dispatcher。
- `sandbox/`：SandboxBackend、HostBackend、路径约束、超时和进程组清理。
- `policy/`：ALLOW/DENY/ASK、默认规则、审计事件。
- `tools/builtin/`：ReadFile、Glob、Grep 等只读工具。
- `cli/`：最小命令行，支持新建会话、Resume、事件流输出。

### 验收

- Fake Provider 能完成“用户问题 → 模型回复”；
- Fake Provider 能完成“模型 Tool Call → Policy → Host ReadFile → Tool Result → 模型总结”；
- Assistant Tool Call 在工具执行前已经落盘；
- 未显式 `unsafe=true` 的 HostBackend 构造和工具执行均失败；
- `run.started`、`tool.started` 包含 Host unsafe 审计字段；
- Ctrl-C 或取消不会留下永久孤儿 Tool Call；
- Unknown Tool、非法参数、路径逃逸和敏感路径均有稳定错误码。

## M3：真实模型与工具执行面

### 任务

- `models/providers/anthropic`：流式文本、工具、推理载荷、重试和 usage。
- `models/providers/openai`：流式文本、工具、reasoning effort 和 usage。
- `models/providers/openai_compatible`：base URL、endpoint capability 配置。
- Model Gateway、角色路由、Fallback 和请求级超时。
- 写文件、编辑、Shell、测试执行工具。
- `policy/`：路径规则、命令规则、审批、allow-once/allow-always、Capability Lease。

### 验收

- 所有 Adapter 通过同一 Provider Contract Test；
- Provider 切换不改变 Core Message/Event 格式；
- 非幂等工具在 Provider 重试时不会自动重放；
- 写工具默认 ASK，Plan 模式拒绝修改；
- 外部文件发生变化后，编辑需要重新读取并拒绝盲写；
- Shell 超时、取消、输出上限和进程组清理均有测试。
- Approval/Capability Lease 以事件持久化；两者只能授权对应 `run_id`，过期或撤销后失效；
  Runtime 恢复 Session 后仍能重建授权投影，默认 ASK 产生可审计的 `approval.requested`。

## M4：上下文、压缩与 Artifact

### 任务

- `context/`：System Prompt、项目上下文、Skill 索引、Memory 注入、Tool Schema 编译。
- Token 估算、模型能力读取和动态预算。
- 大型工具输出外置到 `artifacts/`，上下文仅保留预览与引用。
- L0 输出截断、L1 确定性微压缩、L2 结构化语义压缩。
- 压缩源范围、摘要版本、Prompt Hash、模型和前后 Token 记录。
- Provider Context Length 错误的响应式压缩。

### 验收

- 输入预算计算不产生网络往返；
- 旧 Tool Call/Result 配对不被压缩边界切断；
- 压缩后任务目标、文件变化、验证状态和下一步可恢复；
- 原始事件和压缩前内容仍可审计；
- 200+ 轮模拟会话不会因上下文超限失败；
- Artifact 大小、访问权限和生命周期可控。

## M5：插件、Skill、Hook、MCP 与 Memory

### 任务

- `plugins/`：Manifest、发现、版本、Hash、lockfile、Plugin Host。
- `skills/`：`SKILL.md` frontmatter、分层发现、按需加载和信任策略。
- `hooks/`：生命周期总线、顺序、超时、失败策略、命令/HTTP/Prompt Hook。
- MCP Client、Server Tool Schema 和断线处理。
- `memory/`：Working/Episodic/Semantic/Procedural、候选抽取、Secret 清洗、检索和删除。

### 验收

- 第三方 Plugin 不在主进程直接执行；
- 项目级 Plugin 默认关闭，启用有明确 Trust 记录；
- Hook 无法绕过 Policy 或 Sandbox；
- Skill 正文未加载时不进入模型上下文；
- MCP 工具与内置工具使用同一 Policy/Hook/Sandbox 链路；
- Memory 条目可追溯、可删除、不会把 Secret 写入长期记忆。

## M6：Subagent、Docker 与服务化

### 任务

- `agents/`：TaskSpec、子 Session、Mailbox、预算、深度、并发和取消。
- Git Worktree/只读 workspace 隔离与 Patch Artifact 合并。
- `sandbox/docker`：DockerBackend，网络、文件、资源和 Secret Broker 配置。
- `api/`：可选 FastAPI Session/Run/Approval/Artifact API。
- SQLite 到 PostgreSQL 的 Store 适配和 Worker lease。

### 验收

- 子代理权限集合始终是父任务子集；
- 子代理崩溃后可查询状态并恢复或取消；
- 要求隔离的 Policy Profile 会拒绝 Host，即使 `unsafe=true`；
- Docker 执行事件可与 Host 执行事件统一回放；
- 多 Worker 不会同时拥有同一 Run。

## M7：评估与可观测性

### 任务

- `observability/`：OpenTelemetry Trace、Metrics、结构化日志、成本核算和脱敏。
- `evals/`：Fake/Replay、Provider Contract、Golden Tasks、Coding Tasks、安全测试。
- 事件轨迹导出、离线回放和评分器接口。
- CI 回归门禁：恢复、策略、压缩、工具安全和 Provider 兼容性。

### 验收

- 任意 Tool Call 可定位到 Session、Run、Model Attempt、Policy、Hook 和 Sandbox；
- 能计算 Token、成本、延迟、压缩率、Tool 错误率和策略拒绝率；
- Trace Replay 能复现相同的状态转移；
- 关键安全回归失败时阻止合并。

## 风险与门禁

| 风险 | 门禁 |
|---|---|
| Host 被误认为安全沙箱 | 配置、事件和 UI 同时显示 `unsafe=true`；隔离 Profile 禁止 Host |
| 重试造成副作用重复执行 | Tool 幂等声明、Intent 事件和不可自动重放策略 |
| 取消造成孤儿 Tool Call | 取消修复协议 + 进程重启恢复测试 |
| 上下文压缩丢失任务状态 | 结构化摘要 Schema + 任务回归集 |
| 插件绕过策略 | Plugin Host 隔离 + 工具链路统一入口 |
| Provider 差异泄漏到内核 | Canonical Contract Test + 依赖方向检查 |
