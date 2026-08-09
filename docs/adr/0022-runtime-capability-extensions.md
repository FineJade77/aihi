# ADR-0022：Runtime 能力注入点（Skill 索引与 Memory）

状态：Accepted
日期：2026-08-07
关联：ADR-0004（Skill 按需加载）、ARCHITECTURE §9.3、§11、TASK.md H-01 / H-02

## 背景

`skills/`、`memory/`、`agents/`、`plugins/`、`mcp/` 五个包合计约 4500 行，实现完整、测试齐备，
但**包外零引用**：`RunCoordinator` 的构造参数里没有它们，`ContextCompiler.compile()` 也不接受
Skill 索引或 Memory。ARCHITECTURE §7 声称「Context Compiler 将系统指令、项目约定、Skill 摘要、
记忆…编译成模型请求」，实际编译的只有系统指令和历史消息。

后果不是「还没做」，而是应用要用这些能力就必须自己写编排 —— 也就是在应用层长出第二套 Runtime，
恰好是 AGENTS.md 明令禁止的事。

继续往 `RunCoordinator.__init__` 加参数也不是答案：它已经有 13 个。

## 决策

### 1. 统一的注入点：`RuntimeExtensions`

```text
RuntimeExtensions(
    context_contributors=(...),   # 读：向编译上下文贡献只读段落
    run_recorders=(...),          # 写：观察已完成的 Run，追加自己的审计事件
)
```

两个结构化 Protocol（`ContextContributor` / `RunRecorder`）定义在 `runtime/extensions.py`。
由于 Python 的 Protocol 是结构化类型，**能力包不需要 import runtime**，`runtime` 也不 import
`skills`/`memory`：依赖方向保持单向。新增能力只增加一个 contributor/recorder，不再动构造函数。

### 2. `ContextSection`：领域无关的上下文拼装单元

`context` 包新增 `ContextSection(title, body, source)` 和 `compose_system_prompt()`。
contributor 返回**已渲染的文本段落**，因此 `context` 不需要认识 Skill 或 Memory 的领域类型。
`compile()` 与 `compact_l2()` 都接受 `sections=`，段落计入 Token 预算，
`CompiledContext.system_prompt` 即最终发给模型的提示词。

### 3. 两个方向的失败语义刻意不对称

| | 失败时 | 理由 |
|---|---|---|
| `ContextContributor` | **fail closed**，Run 失败 | 静默丢段落＝模型拿到一个悄悄缺少记忆或 Skill 索引的上下文，属于组合错误 |
| `RunRecorder` | **fail open**，Run 保持成功 | 此时副作用已提交，记录器不得改写既成结果（与 `_notify_observers` 同一原则） |

### 4. Skill 只注入索引

`SkillIndexContributor` 渲染 `name@version (scope): description`，正文一律不进上下文；
加载正文仍需 `SkillLoader` 的 `name@version+scope+content_sha256` Trust 与重新 Hash 校验
（ADR-0004、ARCHITECTURE §9.3）。索引本身不扩大任何工具、Policy 或 Sandbox 权限。

### 5. Memory 读自动、写不自动

- `MemoryContextContributor`：按最近一条用户消息检索作用域内记忆并渲染，受 `MemoryAccess` 约束；
- `MemoryCandidateRecorder`：Run 结束后从助手输出抽取候选，只产生 `memory.candidate` 事件。
  升级为持久记录仍需显式 `MemoryService.write` 和匹配的 `MemoryAccess`（ARCHITECTURE §11）。

`MemoryService.extract()` 新增可选的按调用 `event_sink`：审计目标是**当前 Session**，
不是服务实例的构造期状态，否则一个长生命周期的 Service 无法为多个 Session 正确记账。

### 6. 公共 API 的提升机制生效

`skills` 与 `memory` 现在有了可承诺的组合契约，因此它们的适配器进入顶层 `__all__`
（ADR-0021 后建立的规则：先有 Runtime 注入点，再提升为公共 API）。
`agents`、`plugins`、`mcp`、`evals`、`api`、`cli` 仍不导出。
`tests/contract/test_public_api.py` 同时守住两侧。

## 后果

- 应用层检测到工作区内的 Skill 目录即自动组合 Skill 索引，
  实测索引进入系统提示词、正文不进；
- Memory 需要选择持久 Store 和作用域策略，属于产品决策，应用层暂不默认启用，
  但可通过公共 API 直接组合；
- contributor 在**每次模型请求前**重新求值，因此同一 Session 的第二个 Run 不会看到第一个 Run 的
  段落，长会话中的记忆检索也随最新用户输入更新；
- 尚未接线的能力：**Subagent**（`agents/`）。它不是上下文维度而是「派生子 Run」维度，
  需要独立的预算/权限/工作区下发路径，留作下一步。
