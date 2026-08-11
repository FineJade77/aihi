# ADR-0032：ToolSpec 归属 `aihi.agent.tools`

状态：Accepted
日期：2026-08-11
关联：ADR-0030、ARCHITECTURE §5、TASK H-17

## 背景

`ToolSpec` 描述模型可见的工具定义，同时携带 Agent 执行治理字段：修改性、并发安全、能力、
超时和幂等性。它被 Policy、Context、Tool Registry、Dispatcher、内建工具以及 MCP/Plugin 适配器
共同消费。将实现文件放在 `aihi.agent` 根目录会让工具契约看起来像 Agent Runtime 的顶层状态，
与职责归属不一致。

## 决策

将实现移动到：

```text
aihi.agent.tools.spec
```

其中保留 `ToolSpec` 和 `IdempotencyPolicy`。`aihi.agent.tools` 继续 re-export 两者，
`aihi.agent` 顶层也继续 re-export `ToolSpec`，因此应用层公共导入面不变。

`tools.spec` 是低层 Tool Contract，只依赖 `aihi.models`；Policy、Context 和执行层可以依赖它。
工具执行的 `base`、`registry` 和 `dispatcher` 仍属于更高层。为避免 `tools.spec → tools.__init__ →
dispatcher → policy → tools.spec` 循环，`ToolDispatcher` 与 `DispatchResult` 从 `tools` 包根延迟
导入；内部模块直接从 `aihi.agent.tools.spec` 导入 `ToolSpec`。

模型层的 `ModelToolDefinition` 仍只包含模型可见字段，不能把 Agent 治理字段下沉到 `aihi.models`。

## 后果

- 代码归属与工具领域职责一致；
- 运行时逻辑、事件格式和安全策略不变；
- `from aihi.agent.tools import ToolSpec` 与 `from aihi.agent import ToolSpec` 保持兼容；
- `aihi.agent.tool_spec` 是内部路径，不再保留；应用只能使用两个顶层公共 API；
- layering contract 增加 `tools.spec` 低层节点，防止工具契约与 Policy-aware Dispatcher 混层。

## 未采纳

- **将完整 ToolSpec 移到 `aihi.models`**：治理字段属于 Agent 执行面；
- **把 ToolSpec 定义放在 `tools.__init__`**：会放大包根导入副作用并引入循环依赖；
- **仅增加 re-export 而不移动实现**：无法修正模块职责和内部依赖边界。
