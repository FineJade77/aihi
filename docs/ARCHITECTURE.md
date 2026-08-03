# aiharness 架构设计

**状态**：定稿待实现
**日期**：2026-08-03

---

## 一、定位

aiharness 是一个 **AI Coding Agent 的运行时**——模型之外的全部：手、眼、记忆和安全边界。

对标 Claude Code 一类的产品形态，而不是通用 Agent 编排框架。这个取舍决定了一切：核心难点在**上下文管理、工具执行安全、会话可恢复**，而不在编排抽象的可插拔性。

### 非目标

明确不做的事，写下来是为了防止范围蔓延：

- **不做通用 Agent 编排框架**。没有 ReAct/Planning/Loop 四种"Agent 模式"的抽象层，没有消息总线，没有协作拓扑。多 Agent 通过"subagent 作为一个 Tool"表达，覆盖主管-工人和流水线两类实际用途。
- **不做接口先行**。抽象从跑通的代码里提取，不提前声明。任何没有两个真实实现验证过的接口都是猜测。
- **不做模型训练/评测平台**。eval 是后期的辅助设施，不是架构中心。

---

## 二、分层原则

按**"改不动的" vs "随时能换的"** 分层，而不是按功能域平铺。

### L0 内核 —— 写错了天花板锁死

五个契约。它们的共同点是：一旦定错，所有历史会话、所有工具、所有 UI 都要陪葬。

1. 对话表示（`Message` / `ContentBlock`）—— 决定持久化格式
2. 会话日志（append-only + projection + fork）—— 决定可恢复性与可审计性
3. 事件流（`Event`）—— 决定所有外围组件的耦合方式
4. Provider 协议 + 能力声明 —— 决定多模型是否真的可换
5. Tool 协议（含权限与沙箱的必经点）—— 决定安全边界能否事后加固

**总量目标：不超过 2000 行。** 超了说明抽象错了。

### L1 能力层 —— 加法，删掉重来成本低

工具集、skills、MCP 客户端、hooks、subagent。

### L2 产品层 —— 最后做

TUI、slash commands、主题、聊天平台接入。

---

## 三、目录结构

```
src/aiharness/
  core/          types, events, errors, ids, tokens        # 零依赖
  model/         provider 协议, capabilities, 路由, 重试
    providers/   anthropic/, openai/, openai_compatible/, fake/
  session/       store (jsonl append-only), session, filestate
  context/       system prompt 组装, token 预算, 压缩, 溢出落盘
  tools/         协议, 注册表, builtin/{read,write,edit,bash,grep,glob}
  permission/    模式, 规则, 决策引擎, 会话授权表
  sandbox/       执行后端 (process → seatbelt → container)
  hooks/         生命周期分发
  loop/          agent.py —— 唯一的循环
  cli/           最小 REPL
```

依赖方向严格单向：`core` ← 其他所有；`loop` 依赖大部分；`cli` 只依赖 `loop` 和 `core`。

**禁止**：`core` 依赖任何其他包；任何包 import `model/providers/` 下的具体实现（只能拿到协议）。

---

## 四、核心契约

### 4.1 对话表示

内核只表达**意图**，厂商机制留在适配器。任何以厂商术语命名的字段都是设计错误。

```python
@dataclass(slots=True)
class ContentBlock:
    kind: str

@dataclass(slots=True)
class TextBlock(ContentBlock):
    text: str
    stable_prefix: bool = False
    # ↑ "此处是一个稳定前缀边界"。不是 cache_control。
    #   适配器决定要不要用、怎么用、用几个。

@dataclass(slots=True)
class ThinkingBlock(ContentBlock):
    text: str
    provider: str                          # 产生它的适配器名
    opaque: dict[str, Any] | None = None   # 必须逐字节回传的载荷，内核不解释

@dataclass(slots=True)
class ToolUseBlock(ContentBlock):
    id: str
    name: str
    input: dict[str, Any]

@dataclass(slots=True)
class ToolResultBlock(ContentBlock):
    tool_use_id: str
    content: str
    is_error: bool = False

@dataclass(slots=True)
class ImageBlock(ContentBlock):
    media_type: str
    data: str   # base64
```

**ThinkingBlock 的跨 provider 规则**（唯一一条硬规则）：

```
block.provider == request.provider  → 原样带上，opaque 逐字节不变
block.provider != request.provider  → 整块丢弃
```

内核不知道 "signature" 是什么。Anthropic 的 signature、OpenAI 的 encrypted reasoning、将来任何家的等价物，都存在 `opaque` 里。

**序列化契约**：`to_dict` / `from_dict` 无损往返，因为会话日志就是持久化这些 dict。

```python
@dataclass(slots=True)
class Message:
    role: Literal["user", "assistant", "system"]
    content: list[ContentBlock]
    id: str
    meta: dict[str, Any]   # 自由溯源：{"compacted": True}, {"subagent": "reviewer"}
```

**`ToolResultBatch`**：一次 assistant turn 对应的全部工具结果，作为**不可分割的单元**交给适配器。

```python
@dataclass(slots=True)
class ToolResultBatch:
    results: list[ToolResultBlock]
```

"Anthropic 要求放进同一条 user 消息"、"OpenAI 要求 N 条独立 role:tool 消息"——两个都是适配器内部的渲染规则，loop 不知道。

### 4.2 会话日志

**append-only 日志，不是状态快照。** 活的对话是**日志重放出的投影**。

买到三件事：

- **崩溃安全**：被 kill 最多丢正在写的那条记录
- **可审计**：压缩只缩小模型视野，从不删磁盘。"它当时为什么那么干"永远可回答
- **可 fork**：fork 是前缀重放到新日志，O(prefix)，永不改动父会话

记录类型：

| 类型 | 内容 |
|---|---|
| `session.meta` | 头：cwd、模型角色、harness 版本、父会话 |
| `message` | 一条 `Message` |
| `compaction` | 用一条摘要替换一组 message id |
| `usage` | 每轮 token 计量 |
| `permission` | 一次权限判定（谁、什么、判成什么、依据哪条规则） |
| `checkpoint` | 命名标记，可回退到 |
| `event` | 非对话事件的持久痕迹（hook 决策等） |

`project_messages(records) -> list[Message]` 是唯一的投影函数。`compaction` 记录**就地应用**——摘要落在被移除跨度的原位，不是追加到末尾。

**并发模型**：一个会话一个所有者 loop，单写者。用 `O_APPEND` 追加 + fsync，不需要锁守护进程。读到损坏的尾行（硬 kill 造成）就丢弃尾部，不是丢整个会话。

### 4.3 事件流与流式块 —— 两层，禁止合并

**这是最容易犯的错。** 合并了之后 UI 会开始依赖 provider 的线格式。

**`StreamChunk`（provider 归一化后的线格式，7 种）** —— 只在 `model/` 层内部流动：

```
MessageStart(model)
BlockStart(index, kind)
TextDelta(index, text)
ThinkingDelta(index, text)
ToolInputDelta(index, partial_json)
BlockEnd(index)
MessageEnd(stop_reason, usage)
```

**`Event`（harness 对外的语义事件）** —— loop 产出 `AsyncIterator[Event]`，UI / 日志 / eval / 远程传输**只消费事件**，任何组件不许伸手进 loop 内部：

```
session.started      turn.started        turn.completed      turn.interrupted
text.delta           thinking.delta      message.completed
tool.requested       tool.permission_required                tool.completed
context.compacted    subagent.started    subagent.completed
error
```

适配器只管产 chunk；loop 累积成 `ModelResponse` 并发 Event。

### 4.4 Provider 协议

**不做最小公倍数，做能力声明。** 抽象成交集会立刻丢掉 effort、前缀缓存、推理回放——那正是 coding agent 最吃的东西。

```python
@dataclass(frozen=True)
class Capabilities:
    streaming: bool
    parallel_tools: bool
    reasoning: bool                  # 会返回推理内容
    reasoning_replay: bool           # 推理内容必须原样回传（否则可安全丢弃）
    effort_levels: tuple[str, ...]   # () = 不支持；适配器映射到自家参数
    prefix_caching: bool             # 能利用 stable_prefix 标记（显式或隐式都算）
    inline_system: bool              # 支持会话中途注入系统级指令
    token_counting: bool
    max_context: int
    max_output: int

class Provider(Protocol):
    name: str
    def capabilities(self, model: str) -> Capabilities: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[StreamChunk]: ...
    async def count_tokens(self, req: ModelRequest) -> int: ...
```

**没有一个字段名来自某家厂商。** loop 和 context 层查询能力，**永远不做 `isinstance(provider, XProvider)` 判断**。

四个实现，地位对等：

| provider | 用途 | 说明 |
|---|---|---|
| `fake` | 测试与契约参考 | 脚本化回复、注入错误(429/529/refusal)、注入延迟(测取消)、录制回放 |
| `anthropic` | 主力 | 适配器最厚，见 4.4.1 |
| `openai` | 官方 API | `reasoning_effort` 三档；工具结果 N 条 `role:tool` |
| `openai_compatible` | base_url + key | GLM/Kimi/DeepSeek/Ollama/vLLM。能力**逐 endpoint 配置** |

**真正的参考实现是 `fake`**：它定义契约，另外三个实现契约。契约测试（同一组 `ModelRequest` 跑过全部四个适配器，断言中性层行为一致）对着 `fake` 写。

#### 4.4.1 Anthropic 适配器承担的厂商约束

这些**全部**留在适配器内，一条都不许泄漏到内核：

- 渲染顺序固定 `tools → system → messages`。工具列表在最前面，改一个工具 = 全量缓存失效 → **工具注册表必须按名字排序、序列化确定**（这条约束由适配器提出，注册表满足它）。
- 显式缓存断点上限 **4 个**；每个断点只向前回溯 **20 个 content block**。长 turn（30+ 个 tool_use/tool_result 是常态）会静默 miss。→ **整套断点选择算法在适配器里**：turn 边界优先，长 turn 内每约 15 个 block 补一个，超上限淘汰最旧的。
- Opus 5 最小可缓存前缀 512 token；低于此静默不缓存（`cache_creation_input_tokens: 0`，无报错）。
- 全部 `tool_result` 必须在**同一条 user 消息**里。拆开会静默地训练模型不再并行调用工具。
- `stop_reason: "pause_turn"` → 映射到中性的 `paused`。
- 会话中途 `{"role": "system"}` 消息（Opus 5 支持，无需 beta header）→ `inline_system=True`。不支持时降级为 user 消息里的 `<system-reminder>` 文本块。
- `thinking: {"type": "adaptive", "display": "summarized"}`；`display` 默认是 `omitted`（空文本），要展示推理必须显式打开。
- `output_config.effort`：`low/medium/high/xhigh/max`。**没有** `budget_tokens`，**没有** `temperature/top_p/top_k`（传了 400）。
- `max_tokens` 是"思考 + 回复"的硬上限。高 effort 下必须给足余量（≥64K）。

### 4.5 Tool 协议

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    concurrency_safe: bool     # 能否与其他 safe 工具并发
    mutates: bool              # 是否改变外部状态（影响权限默认值）

class Tool(Protocol):
    spec: ToolSpec
    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
```

`concurrency_safe` 必须在**协议里**，不能靠名字猜。read/grep/glob 并发跑；write/edit/bash 串行。

`ToolContext` 携带：cwd、session、file state tracker、sandbox、取消令牌、发事件的通道。

**权限判定不在工具内部**，在 loop 里、执行前。见第六节。

---

## 五、Agent Loop

唯一入口：

```python
async def query(session: Session, prompt: str, *, deps: Deps) -> AsyncIterator[Event]
```

### 5.1 主流程

```
emit TurnStarted
loop (最多 max_iterations 轮):
    组装 ModelRequest（system + messages + tools + effort）
    provider.stream(req) → 累积 StreamChunk，同时 emit TextDelta/ThinkingDelta
    emit MessageCompleted
    session.add_message(assistant_msg)          ← 必须先落盘，再执行工具

    if stop_reason in (end_turn, refusal, max_tokens): break
    if stop_reason == paused: continue          ← 不需要新用户输入

    # stop_reason == tool_use
    for each tool_use:  权限判定 → allow / deny / ask
    并发执行 concurrency_safe 的，串行执行其余的
    收集成 ToolResultBatch → session.add_message(user_msg)
emit TurnCompleted
```

**顺序不可换**：带 `tool_use` 的 assistant 消息**必须在执行工具前落盘**。否则崩溃后日志里会出现"工具执行了但没有记录"的空洞。

### 5.2 中断修复协议 —— 最容易写错的一段

每一轮跑在独立的 `asyncio.Task` 里，Esc 取消它。捕获 `CancelledError` 后**必须按序完成**：

1. 取消所有在飞的工具任务，`gather(..., return_exceptions=True)` 收尾
2. 对每一个**还没有配对 tool_result 的 tool_use**，合成 `is_error: true` 的结果（`"Interrupted by user"`），**全部放进同一个 `ToolResultBatch`**
3. 追加一条中断标记消息，让模型下一轮知道发生过什么
4. emit `TurnInterrupted`，落盘

**这段修复逻辑本身必须屏蔽二次取消**（`asyncio.shield`，或把它放在不可取消的路径上）。

> **失败模式**：用户连按两次 Esc，修复只做了一半 → 日志里出现没有配对 `tool_result` 的 `tool_use` → 这个会话之后每次请求都 400，**永久报废**。
>
> 这就是为什么中断必须是 L0 的一部分，不能事后补。

---

## 六、权限模型

判定发生在 **loop 里、工具执行前**，顺序固定：

```
mode → deny 规则 → allow 规则 → 会话授权表 → hooks → ask 回调
```

**模式**：`default` / `acceptEdits` / `plan` / `bypass`

**ask 回调**（注入进 loop，不是双向 generator——后者在并行工具下控制流没法推理）：

```python
async def can_use_tool(name: str, input: dict, ctx: PermissionContext) -> Decision
```

`Decision` = `allow` | `deny(reason)` | `allow_always(rule)`

`allow_always` 写进**会话授权表**（例："以后 `bash` 里匹配 `npm test` 的都放行"）。需要一个基于工具名 + 输入模式的匹配器。

**每一次判定都写进 session log**（`permission` 记录类型）。这是审计线索，也是"它当时为什么敢删那个文件"的唯一答案。

---

## 七、上下文管理

三个机制，常被混为一谈，**必须分开做**：

| 机制 | 触发点 | 成本 | 说明 |
|---|---|---|---|
| **工具结果截断/溢出** | tool 边界 | 零 LLM 调用 | 2MB 的文件读取不进上下文。截断到 N 字符 + 全文落盘 + 告诉模型路径。**性价比最高的一层** |
| **旧工具结果清理** | 阈值 | 零 LLM 调用 | 优先用 provider 的服务端能力；无则自己做 |
| **压缩** | 总量 > 阈值 | 一次廉价模型调用 | 用摘要替换旧跨度。必须保留：原始用户意图、最近的文件编辑、当前任务状态 |

**`Compactor` 是协议**，默认实现委托给 provider 的服务端压缩（若 capability 支持），需要时换成自己的实现。

**token 计量分两条路**，故意分开：

- `estimate_tokens()` —— 廉价、同步、无网络。压缩触发器每轮调用，差 10% 无所谓，网络往返不可接受
- `TokenCounter` —— provider 精确计数，用于代价高的决策（预算预检、eval 报告）。计数失败降级到估算，**绝不让计数打断一个 turn**

> 绝不用外来分词器（`tiktoken` 等）。对 Claude 的计数是错的，代码上错得尤其厉害。

`stable_prefix` 标记由上下文层在**语义边界**（turn 结束、system prompt 末尾）打，**数量不设限**。厂商配额是适配器的事。

---

## 八、配置

分层，从低到高：内置默认 < 用户 `~/.aiharness/settings.json` < 项目 `.aiharness/settings.json` < 本地 `.aiharness/settings.local.json`（gitignore）< 环境变量 `AIH_*` < 显式覆盖。

dict 深合并；**list 整体替换**——项目声明 `policy.deny` 意思是"就这些规则"，不是"这些加上用户的"，否则 deny 列表永远收不窄。

模型配置：**role → provider + model**。路由解析角色，代码里永远不出现裸模型 id，换模型是配置改动不是代码改动。

```yaml
model:
  providers:
    anthropic:  {type: anthropic, api_key_env: ANTHROPIC_API_KEY}
    glm:        {type: openai_compatible, base_url: "...", api_key_env: GLM_API_KEY,
                 capabilities: {reasoning: true, prefix_caching: false, effort_levels: []}}
    fake:       {type: fake}
  roles:
    primary:    {provider: anthropic, model: claude-opus-5, effort: xhigh}
    fast:       {provider: anthropic, model: claude-haiku-4-5}
    compactor:  {provider: anthropic, model: claude-haiku-4-5}
  fallbacks:
    primary: [{provider: glm, model: glm-4.6}]
```

---

## 九、设计决策记录

| # | 决策 | 理由 | 代价 |
|---|---|---|---|
| 1 | 垂直切片优先，不做接口先行 | 抽象要从跑通的代码里提取。自顶向下定 9 大类接口，会在真实调用面前发现抽象是错的 | 通用能力晚半拍 |
| 2 | 内核只表达意图，厂商机制在适配器 | 以厂商术语命名字段 = 内核长成那家的形状，别人来适配 | Anthropic 适配器变厚（正确的地方厚） |
| 3 | 通用 provider 层，四个实现 | `fake` 的测试价值单独就够本；OpenAI 兼容层是国内模型刚需 | ThinkingBlock 跨 provider 不可通约，靠 `provider` 字段丢弃解决 |
| 4 | 全 async | 流式 + 并行工具下，同步版本会变成线程池噩梦 | 所有工具必须 `async def` |
| 5 | 权限用注入回调，不用双向 generator | 可测试、可组合，CLI 和未来 HTTP server 都能实现；双向 generator 在并行工具下没法推理 | — |
| 6 | subagent 是一个 Tool，不做消息总线 | 覆盖主管-工人和流水线两类实际用途，不需要总线/订阅/心跳 | 复杂拓扑要等真实需求 |
| 7 | 断点策略在适配器，不在 context 层 | "最多 4 个"、"20-block 窗口" 是 Anthropic 配额，泄漏进内核是设计错误 | — |
| 8 | 中断修复属于 L0 | 修复做错会让会话永久 400 报废，事后补不进去 | S1 就要写 |
| 9 | 编辑工具要 "读后才能写" 不变式 | 否则 agent 会盲写覆盖用户改动。需要跨工具共享的 file state tracker | tracker 是 L0 组件 |

---

## 十、参考

- `docs/TASKS.md` —— 分阶段任务分解与验收标准
