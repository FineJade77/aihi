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

压缩记录源事件范围、前后 Token、模型、Prompt Hash、策略版本和摘要版本。边界不能切断
Tool Call/Tool Result 配对。
