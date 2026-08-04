# RFC-0002：Context Compiler 与自动压缩

- 状态：Accepted
- 日期：2026-08-04

## 问题

Coding Agent 的上下文包含系统指令、工具 Schema、项目规则、文件内容、命令输出、历史消息、
Skill 和 Memory。直接把事件历史全部发送给模型会导致超限、成本失控和任务状态丢失。

## 方案

`context/` 将事实历史编译为版本化 Context View。源历史永远保留，Context View 可以替换。

### 压缩级别

1. `L0`：大型 Tool Result 写入 `artifacts/`，上下文只保留预览和引用；
2. `L1`：确定性清理旧工具结果、重复系统上下文和无效进度；
3. `L2`：Compact Model 生成结构化摘要，并通过 Schema 校验。

### 摘要字段

```text
objective
constraints
decisions
files_changed
verified_state
open_questions
next_steps
permission_mode
skills
subagents
artifacts
```

### 触发

```text
usable_input = context_window - reserved_output - tool_schema - safety_margin
```

达到配置阈值时主动压缩；Provider 返回 Context Length 错误时最多执行一次响应式压缩，
防止无限重试。

当前 L0/L1 基线由 `ContextCompiler` 在无网络往返下执行：工具结果超过阈值时写入可寻址
ArtifactStore，只向模型保留预览和引用；预算无法通过成对消息压缩满足时返回稳定的
`context_window_exceeded` 错误。L2 通过可注入的 `SummaryGenerator(SummaryRequest) ->
StructuredSummary` 协议生成固定 Schema 摘要；默认 `DeterministicSummaryGenerator` 不发起
网络请求，未来可由 Compact Model 适配器替换。确定性压缩追加 `compaction.created`，Artifact
写入追加 `artifact.created`；原始消息事件保持不变。

Provider 适配器将 HTTP 413、明确的 context/token-limit 错误和流内错误事件归一化为稳定的
`provider_context_length`。Runtime 对每个 Run 只允许一次响应式 L2 压缩重试；第二次仍返回该
错误时直接失败，避免无限重试。`compaction.created.trigger` 标记 `budget`、
`preflight_context_window` 或 `provider_context_length` 触发来源。

压缩记录源事件范围、前后 Token、模型、Prompt Hash、策略版本和摘要版本。边界不能切断
Tool Call/Tool Result 配对。
