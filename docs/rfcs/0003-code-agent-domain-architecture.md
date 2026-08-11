# RFC-0003：Coding Agent 领域架构

- 状态：Draft
- 日期：2026-08-11
- 关联：`docs/rfcs/0001-runtime-architecture.md`、`docs/adr/0033-code-agent-cli-bridge.md`、
  `docs/adr/0004-skill-on-demand-loading.md`、`docs/adr/0032-tool-spec-ownership.md`

## 摘要

`aihi.code_agent` 目前只承担「拼装」职责：把 Provider、Sandbox、Tool、Skill 组合成一个
`Runtime`，其余全部推给配置或调用方。结果是这个 Coding Agent 没有自己的领域 API——没有流式
对话接口、没有 coding 系统提示词、没有内置 Skill，工具集也散落在三处由一个硬编码字典拼合。

本 RFC 为 `aihi.code_agent` 补上领域层：类型化的 Turn 事件流、声明式工具集注册表、分层
system prompt、随包发布的内置 Skill，以及命名 Subagent 类型。`aihi.models` 不改动；
`aihi.agent` 仅需一处受限扩展，理由见 §设计.5。

## 问题

以下四点均经代码核实，不是推测。

**1. 没有领域级流式接口。** `Coordinator` 只有 `run()` 和 `resume()`，返回终态 `RunResult`；
增量只能经 Session observer 这条 side channel 逃逸。每个消费方都要自己接 observer 并按 wire
形状解析，例如靠 `data.kind == "text_delta"` 认文本增量。TUI 因此长期不显示回答正文，
`approval.requested` 事件虽然一直在推送却无人消费。

**2. 没有 coding 系统提示词。** `CodeAgentConfig.system_prompt` 默认 `""`，
`Coordinator.run(system_prompt="")` 默认也是空串，包内不含任何 prompt 资产。开箱即用时这个
Coding Agent 不带任何领域提示词，每个用户必须自备。

**3. 工具集定义是散的。** 大部分工具来自 `aihi.agent`，Git 工具在 `coding_tools.py`，
`load_skill` 在 `skills.py`，最终由 `runtime.py:_build_tools` 内的硬编码 `factories` 字典
拼装。新增一个工具必须修改 `runtime.py`，且 `tool.list` 无法从单一来源报告描述与 schema。

**4. 没有内置 Skill。** `SkillScope.BUILTIN` 枚举早已存在却无人使用；`skill_roots` 完全由
配置驱动，且 `runtime.py` 中「有 skill_roots 就必须有 trust lockfile」的检查会强制每个用户
先配置锁文件才能使用任何 Skill。

**5. Subagent 没有类型，且跑在空提示词下。** `SubagentTool` 是单一泛化工具（`name="task"`，
输入仅 `objective` 与预算/能力），子 Run 没有身份、没有专属工具子集、没有专属模型。
`ChildRunSubagentRunner` 本身支持 `system_prompt` 参数，但 `RuntimeBuilder._subagent_tool()`
构造它时未传入，因此**所有子 Agent 一律以空 system prompt 运行**。`config.subagents.enabled`
默认为 `False`，且整个 Subagent 面未被 §设计.1 的 Turn 事件模型覆盖——尽管
`subagent.spawned` / `started` / `completed` 三个事件早已在发布。

## 设计

### 1. 领域 Turn 事件模型（新增 `turns.py`）

领域层发布类型化事件，消费方不再接触 wire 形状：

```python
@dataclass(frozen=True, slots=True)
class TurnEvent:
    seq: int | None
    run_id: str | None

class TextDelta(TurnEvent):          text: str
class AssistantMessage(TurnEvent):   text: str; message: Message
class ToolCallStarted(TurnEvent):    call_id: str; tool_name: str; input: JsonObject
class ToolCallFinished(TurnEvent):   call_id: str; tool_name: str; is_error: bool; preview: str
class ApprovalRequested(TurnEvent):  approval_id: str; tool_name: str | None; scope: str; reason: str | None
class RunStateChanged(TurnEvent):    state: str
class TurnFinished(TurnEvent):       result: RunResult
```

`CodeAgentRuntime` 增加流式入口：

```python
async def stream(
    self, session: Session, *, user_message: str, run_id: str | None = None, ...
) -> AsyncIterator[TurnEvent]: ...
```

实现不改 `aihi.agent`：内部把 Session observer 接入 `asyncio.Queue`，`coordinator.run()` 作为
task 并发执行，迭代器消费队列。`finally` 中摘除 observer。

**顺序不变式：`TurnFinished` 必须是最后一个事件，且在它之前队列已排空。** 现有 Worker 的
`finish_runs` 先写响应再 flush 通知，消费方会在拿到终态后才收到该 Run 的事件；把这条保证
下沉到领域层后，所有消费方（Worker、Eval、嵌入方）一次性受益。

`run()` 与 `resume()` 保留现有签名，但重实现为「消费 `stream()` 直到 `TurnFinished`」，
避免两条并行代码路径。取消经 `cancel_event`；提前关闭迭代器同样触发取消。

### 2. 工具集注册表（新增 `tools/` 子包）

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    factory: Callable[[ToolBuildContext], Tool]
    default_enabled: bool = True
    requires: tuple[str, ...] = ()      # 例如 ("skill_loader",)

CODING_TOOLSET: tuple[ToolDefinition, ...]
```

`ToolBuildContext` 携带 `config`、`skill_loader` 等构造依赖。`build_tools()` 按
`config.tools` 允许清单过滤，并校验 `requires` 已满足。

新增工具 = 追加一条 `ToolDefinition`，不再修改 `runtime.py`；`tool.list` 从同一来源报告。

### 3. 分层 system prompt（新增 `prompts/`）

内置提示词以包数据 markdown 存放（`prompts/coding.md`），可 review、可 diff，不做成 Python
字符串字面量。有效提示词按层组合：

```text
内置 coding prompt
  + 环境段（cwd、平台、Git 分支、当前可用工具与 Skill 目录）
  + 项目约定（工作区内的 AGENTS.md / CLAUDE.md，若存在）
  + 用户配置 agent.system_prompt
```

新增 `agent.system_prompt_mode`，取值 `append`（默认）或 `replace`。

> **行为变更**：现状下配置 `system_prompt` 等同于「全部内容」。改动后默认变为在内置提示词
> 之后追加。需要完全替换的用户显式设置 `system_prompt_mode = "replace"`。

### 4. 内置 Skill（新增 `skills/builtin/`）

首批：`code_review.md`、`debug.md`、`test_writing.md`、`refactor.md`。以
`SkillScope.BUILTIN` 注入到 `config.skill_roots` 之前，遵循 ADR-0004 的渐进披露：系统提示词
只列出名称与描述，正文由既有 `load_skill` 工具按需加载，不预先灌入上下文。

**信任边界**：BUILTIN 作用域隐式受信，不需要 trust lockfile。内置 Skill 随 distribution 发布，
其完整性即该包的完整性——包若被篡改，`runtime.py` 早已执行。`USER` / `PROJECT` / `WORKSPACE`
作用域的显式信任要求保持不变。因此需放宽 `runtime.py` 中的锁文件检查，使其只约束非 BUILTIN
作用域。

### 5. 命名 Subagent 类型（新增 `subagents/`）

与工具集同构的声明式注册表，每个类型自带提示词、工具子集、能力集与模型：

```python
@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    name: str                          # "explore" / "code_review" / "test" / "general"
    description: str                   # 进入 task 工具 schema，供模型选择
    prompt: str                        # 专属 system prompt，来自包数据 markdown
    capabilities: frozenset[str]       # 只能收窄父 Run 的 SubagentAuthority
    tools: tuple[str, ...] | None = None   # None 表示继承父注册表（仍按 capabilities 收窄）
    model: str | None = None           # None 表示沿用 config.subagents.model
    budget: AgentBudget | None = None  # None 表示沿用父授权预算

CODING_SUBAGENTS: tuple[SubagentDefinition, ...]
```

首批：`explore`（只读检索，仅 `filesystem.read`，无 bash/write）、`code_review`（只读加
`git_diff` / `git_status`）、`test`（可执行 bash 跑测试）、`general`（继承父能力）。

`task` 工具输入增加 `agent_type` 枚举字段，默认 `general`。code-agent 为每个定义构造一个
带专属 `system_prompt` 与模型的 `ChildRunSubagentRunner`。

#### 为什么需要扩展 `aihi.agent`

`SubagentTool` 的 `spec` 固定为 `name="task"`，且每个实例自持
`self._graphs: dict[(session, run), (TaskGraph, root)]`。若在应用层为每个类型各建一个
`SubagentTool` 实例，同一个父 Run 会得到 N 张互不相干的 `TaskGraph`，
`max_children` 与 `max_depth` 将**按类型分别计数而非全局计数**，绕开 ADR-0008 的任务治理上限。
替代做法是在 `code_agent` 内自行实现 spawn / transition 包装，那等于把任务治理搬进应用层，
与 ADR-0030 的分层相悖。

因此本 RFC 提出一处受限扩展：`SubagentTool` 接受**命名 Runner 映射**，单实例、单 TaskGraph，
按 `agent_type` 分派：

```python
SubagentTool(
    runners: Mapping[str, SubagentRunner],   # 取代单个 runner；"general" 为必需键
    *, authority: SubagentAuthority,
)
```

同时 `RuntimeBuilder.with_subagents()` 增加 `runners` 参数以传入该映射。单 Runner 形态保留为
兼容路径。治理留在 harness，类型定义留在应用——边界不变。

#### 事件与配置

§设计.1 的事件模型补齐 Subagent 面，使 Task Graph 不再只能靠轮询 `task.list` 刷新：

```python
class SubagentSpawned(TurnEvent):    task_id: str; agent_type: str; objective: str
class SubagentStarted(TurnEvent):    task_id: str; agent_type: str
class SubagentCompleted(TurnEvent):  task_id: str; state: str; summary: str | None
```

配置扩展 `[subagents]`，允许按类型覆盖模型与开关，未列出的类型沿用默认定义：

```toml
[subagents]
enabled = true
model = "deepseek-chat"

[subagents.types.explore]
model = "deepseek-chat"

[subagents.types.test]
enabled = false
```

> **实测更正**：`subagents.enabled` 保持 `False`（默认开启会让所有不传 EventStore 的
> `CodeAgentRuntime.create()` 失败，波及每个嵌入方）。此外类型声明的 capabilities 与
> tools 是**强制执行**的，不只是描述。原文如下，保留以便对照：
> ~~`subagents.enabled` 默认由 `False` 改为 `True`~~，但默认授权收敛为只读——
> `capabilities = {"filesystem.read"}`、`max_depth = 1`、`max_children = 3`。即开箱可用的
> 委派只有 `explore` 与 `code_review` 两类只读子 Agent；`test` 等需要副作用的类型必须显式开启。

## 模块布局

```text
code_agent/
    __init__.py
    config.py
    runtime.py            仅负责拼装
    turns.py              Turn 事件模型与 stream()
    prompts/
        __init__.py       compose_system_prompt()
        coding.md
    tools/
        __init__.py       CODING_TOOLSET 与 build_tools()
        registry.py       ToolDefinition / ToolBuildContext
        git.py            由 coding_tools.py 迁入
        skill.py          由 skills.py 迁入
    skills/
        __init__.py       builtin_skill_root()
        builtin/*.md
    subagents/
        __init__.py       CODING_SUBAGENTS 与 build_runners()
        registry.py       SubagentDefinition
        prompts/*.md      每个类型的专属提示词
    framing.py
    protocol.py
    worker.py
```

`coding_tools.py` 与 `skills.py` 迁移后删除；两者当前均未被 `aihi.code_agent.__all__` 导出，
外部导入面不受影响。

## 打包

**实测更正。** hatchling 的 `packages = ["src/aihi"]` **已经**打包 `.md` 资产，无需声明
`artifacts`——本 RFC 早期版本称「不加就不会进 wheel」是错的。构建真实 wheel 后 9 个资产
（1 个 coding 提示词、4 个内置 Skill、4 个 Subagent 提示词）全部存在。

因此不增加 `artifacts` 配置（那只是空转），改以一条打包断言测试作为回归防线：
`tests/packaging/test_code_agent_assets.py` 构建 wheel 并断言三类资产路径均在产物中。

## 不变式

新增：

- `stream()` 以 `TurnFinished` 结束，且此前该 Run 的全部事件均已产出。
- 内置 Skill 正文仍经 `load_skill` 显式加载，不自动进入上下文。
- 一个父 Run 对应且仅对应一张 `TaskGraph`，与被调用的 `agent_type` 数量无关。
- Subagent 类型只能收窄父 `SubagentAuthority`，不能拓宽。

保持：

- Event Log 是事实源；流式 Delta 是 ephemeral，不进 Event Store。
- 非 BUILTIN 作用域的 Skill 仍需显式信任。
- 所有副作用仍走 `tools → policy → hooks → sandbox`。
- 任务治理（depth / children / 预算上限）留在 `aihi.agent`，不下放应用层。

变更：

- `agent.system_prompt` 默认语义由「替换」变为「追加」。
- BUILTIN 作用域 Skill 不再要求 trust lockfile。
- `subagents` 默认授权收敛为只读（`max_depth = 1`、`max_children = 3`）；`enabled` 仍为 `False`。

## 分期

**本轮（领域层）**：`turns.py`、`tools/`、`prompts/`、`skills/`、`subagents/`，`runtime.py`
收敛为拼装，打包资产与测试。含 `aihi.agent` 中 `SubagentTool` 命名 Runner 映射这一处扩展
（见 §设计.5），单 Runner 形态保留兼容。Worker 现有命令面与 CLI 保持可用——Worker 内部改为
消费 `stream()`，对外协议不变；新增的 Subagent 事件经既有 `event` 通知发布，Task Graph 面板
无需改协议即可从轮询转为事件驱动。

**下轮（协议层，本 RFC 非目标）**：`run.start` 由阻塞式请求改为非阻塞 `turn.submit`，
事件流承载进度，终态由事件而非响应给出。该轮需同时修复
`apps/aihi-code-cli/src/rpc/client.ts:151` 的 `requestTimeoutMs ?? 30_000`——它作用于每一个
请求，包括阻塞式的 `run.start`，任何超过 30 秒的 Run 都会在客户端超时而 Worker 仍在执行。

## 风险

- **提示词回归无门禁**：prompt 变更不会被类型或单测捕获。缓解：为 `compose_system_prompt()`
  写快照测试，覆盖分层顺序与 `replace` 模式。
- **内置 Skill 与项目 Skill 重名**：由既有 `SkillScope.priority` 决定优先级，项目作用域覆盖
  内置。需为该覆盖路径补测试。
- **`worker.py` 已达 1314 行**：本轮会再向其中注入 `stream()` 消费逻辑。建议后续拆分为
  `worker/server.py` 与 `worker/commands/`，但不阻塞本轮。
- **Subagent 默认开启放大成本与失控面**：即便默认只读，模型仍可能过度委派而消耗预算。缓解：
  默认 `max_depth = 1`、`max_children = 3`，并为「授权只能收窄」与「单 Run 单 TaskGraph」
  两条不变式各写一条测试——后者正是命名类型引入的新失效模式。
- **`SubagentTool` 扩展的兼容性**：单 Runner 构造形态必须保留，否则 `aihi.agent` 的现有
  嵌入方会破。需为两种构造形态各留测试。
