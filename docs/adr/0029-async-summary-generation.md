# ADR-0029：异步 SummaryGenerator 与模型驱动压缩

状态：Accepted
日期：2026-08-07
关联：ARCHITECTURE §7、ADR-0028（ModelRoles 只定义有消费者的角色）

## 背景

`SummaryGenerator.generate()` 是同步方法，因此 L2 压缩只能用确定性摘要器 ——
一个模型驱动的 compact 适配器**无法接入**。长会话的上下文质量因此有天花板：
`DeterministicSummaryGenerator` 只能抽出最后一条用户消息作为 objective。

这也是 ADR-0028 里 `ModelRoles` 刻意不定义 `compact` 角色的原因：没有任何代码能消费它。

## 决策

### 1. 只把需要的那一段改成 async

`compile()`（L0/L1）是纯计算，不改。**只有 `compact_l2()` 需要生成器**，因此只有它变成
`async def`。调用方 `RunCoordinator._loop` 本来就是 async，两处 `await` 即可。

这与 ADR-0021 的立场一致：不为 async 而 async。这里有真实的并发对象——模型请求——
才值得改；单机 SQLite 写入没有，所以 Store 保持同步。

`SummaryGenerator.generate` 因此是 `async def`；离线实现不 await 任何东西。

### 2. `ModelSummaryGenerator`：全程防御

- **输入有界**：发送前按 `max_input_chars` 截断，保留尾部（近期轮次承载状态）。
  压缩本身不该需要压缩；
- **输出必须落回同一 schema**：容忍 JSON 外的散文与代码围栏，但 objective 缺失或非字符串即判失败；
- **任何故障都降级**，不让 Run 失败：压缩失败等于 `ContextWindowExceeded`，
  **较差的摘要一定好过没有摘要**；
- 事实归编译器所有：`artifacts` 和 `omitted_message_count` 由编译器填，不采信模型自述。

### 3. 降级必须留痕

`StructuredSummary` 新增 `strategy` 字段，随 payload 一起进入 `compaction.created` 事件：

| strategy | 含义 |
|---|---|
| `l1_deterministic` | L1 确定性微压缩（不变） |
| `l2_deterministic` | L2 离线摘要器 |
| `l2_model` | compact 模型产出 |
| `l2_model_fallback` | compact 模型失败，已降级 |

`CompactionRecord.strategy` 从硬编码的 `l2_structured` 改为取自摘要本身。
一个"配了 compact 模型但从来没成功过"的部署，会在自己的事件日志里显形。

### 4. `compact` 角色现在有消费者

`ModelRoles` 新增 `compact`（未设置时回落到 primary）。`vision`/`memory`/`judge` 仍不定义 ——
同一条规则：没有消费者的角色是兑现不了的承诺。

应用层配置压缩模型即启用；不配置时用离线摘要器，
**压缩因此不依赖第二个模型可达**。

## 后果

- 事件负载新增 `strategy` 字段（加法变更），并且 L2 的 `strategy` 取值从 `l2_structured`
  变为三个更精确的值。无已发布消费者，不升 `EVENT_SCHEMA_VERSION`；
- `compact_l2()` 变为 async 是公共契约变更，调用方需 `await`；
- 压缩现在可能产生一次额外的模型调用与费用。它只在 L2 触发，即 L1 已无法满足预算时。
