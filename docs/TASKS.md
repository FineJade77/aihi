# aiharness 任务分解

配套 `docs/ARCHITECTURE.md`。每个任务给出**验收标准**——没有验收标准的任务不算任务。

**总原则**：S1 + S2 是真正的 MVP（能完成"读代码 → 改文件 → 跑测试"一个完整回路）。S3 是让它从 demo 变成能用的东西。S4 之前不碰 TUI。

---

## S0 · 项目骨架

| ID | 任务 | 验收 |
|---|---|---|
| S0-1 | `pyproject.toml`：hatchling、py3.11+、依赖 `anthropic` / `httpx` / `pyyaml`，dev 依赖 `pytest` / `pytest-asyncio` / `ruff` / `mypy` | `pip install -e ".[dev]"` 成功 |
| S0-2 | 目录骨架 + 各包 `__init__.py`，按 ARCHITECTURE §3 | `ruff check` 通过 |
| S0-3 | CI：ruff + mypy + pytest 三件套 | 本地 `make check` 一条命令跑完 |
| S0-4 | 依赖方向守卫测试：`core` 不 import 任何其他包；任何包不 import `model/providers/` 下的具体实现 | 一个 AST 扫描测试，违反即失败 |

---

## S1 · 最小可跑通

**目标**：能对话、能读文件、杀掉进程后能续跑。中断修复路径在 `fake` provider 上有单测覆盖。

### core/

| ID | 任务 | 验收 |
|---|---|---|
| S1-1 | `core/ids.py`：带前缀、时间有序、短的 id（`ses_` / `turn_` / `msg_` / `toolu_`） | 1e5 次生成无碰撞；字典序 == 时间序 |
| S1-2 | `core/errors.py`：错误分类，每个带稳定 `code` 和 `retryable` | 调用方可按 code 分支，不靠消息文本 |
| S1-3 | `core/types.py`：`ContentBlock` 全族 + `Message` + `ToolResultBatch` + `ModelRequest` / `ModelResponse` + `Usage` | **往返测试**：任意 Message → `to_dict` → `from_dict` 完全相等，含 `ThinkingBlock.opaque` |
| S1-4 | `core/events.py`：全部 Event 类型 + `to_dict` | 每个 Event 可序列化成 JSON 一行 |
| S1-5 | `core/tokens.py`：`estimate_tokens()` 廉价估算 + `TokenCounter` 精确计数（含降级） | 估算与真实计数偏差 <20%；provider 抛异常时 counter 不抛，退回估算 |

### model/

| ID | 任务 | 验收 |
|---|---|---|
| S1-6 | `Provider` 协议 + `Capabilities` + 7 种 `StreamChunk` | 类型定义，无实现 |
| S1-7 | `providers/fake/`：脚本化回复、注入错误、注入延迟、录制回放 | 能脚本出"文本回复"、"单工具调用"、"并行三工具"、"中途 429"、"卡住 10s"五种剧本 |
| S1-8 | `providers/anthropic/`：streaming、tool_use、thinking、effort、`pause_turn` 映射 | 真实 API 打通一次对话；`stop_reason` 五种都能正确映射 |
| S1-9 | 契约测试套件：同一组 `ModelRequest` 跑过所有已实现适配器，断言中性层行为一致 | `fake` 与 `anthropic` 都通过 |
| S1-10 | 角色路由：role → (provider, model)，含重试与 fallback | `primary` 挂掉自动切 fallback，事件流里可见 |

> `providers/openai/` 和 `providers/openai_compatible/` 排到 S4——先让契约在两个实现上稳住。

### session/

| ID | 任务 | 验收 |
|---|---|---|
| S1-11 | `store.py`：JSONL append-only，`create` / `append` / `read` / `meta` / `list` / `fork` | 损坏尾行只丢尾部不丢会话；1000 条记录追加不退化成 O(n²) |
| S1-12 | `project_messages()`：日志重放成消息列表，`compaction` 就地应用 | 摘要落在被替换跨度的原位而非末尾 |
| S1-13 | `session.py`：唯一的写入方，投影 + 写穿 | 模型看到的与磁盘上的不可能漂移（属性测试） |
| S1-14 | `InMemorySessionStore`：同契约无磁盘 | 与 JSONL 版跑同一组测试 |

### loop/

| ID | 任务 | 验收 |
|---|---|---|
| S1-15 | `query()` 主流程：组装 → 流式 → 累积 → 落盘 → 工具 → 循环 | 带 `tool_use` 的 assistant 消息**在执行工具前**落盘（测试断言写入顺序） |
| S1-16 | **中断修复协议**（ARCHITECTURE §5.2 四步） | 在 fake 的"卡住 10s"剧本上取消，断言：① 无孤儿 `tool_use` ② 连续两次取消后日志仍合法 ③ 续跑后下一次请求不报错 |
| S1-17 | 并发执行编排：`concurrency_safe` 并发、其余串行，结果收成一个 `ToolResultBatch` | fake 的"并行三工具"剧本下三个工具真并发（时间断言） |

### 其余

| ID | 任务 | 验收 |
|---|---|---|
| S1-18 | `config/`：分层加载、深合并、list 整体替换、`AIH_*` 环境覆盖 | 六层优先级各有一个测试 |
| S1-19 | `tools/`：协议 + 注册表（**按名字排序、序列化确定**） | 同一组工具两次序列化字节相同 |
| S1-20 | `read` 工具：行号、offset/limit、二进制拒绝、超长截断 | 读 2MB 文件不进上下文，返回截断提示 + 落盘路径 |
| S1-21 | `cli/`：最小 REPL，消费 Event 流打印，Ctrl-C 触发中断 | `aih` 能跑起来对话 |
| S1-22 | `--resume <session_id>` | 杀掉进程后续跑，历史完整 |

**S1 出口标准**：`aih` 里问"这个项目有哪些文件"，它能读、能答；Ctrl-C 打断后会话仍可续跑；`pytest` 全绿且中断路径有覆盖。

---

## S2 · 能写代码

**目标**：完成"读代码 → 改文件 → 跑测试 → 报告结果"的完整回路，带权限确认。

| ID | 任务 | 验收 |
|---|---|---|
| S2-1 | **file state tracker**：本会话读过哪些文件、当时的 mtime/hash（L0 组件，跨工具共享） | 外部修改文件后，编辑被拒绝并提示重读 |
| S2-2 | `edit` 工具：字符串替换 + **唯一性检查** + **读后才能写**不变式 | 匹配 0 次或 >1 次都报错；未读过的文件拒绝编辑 |
| S2-3 | `write` 工具：覆盖前必须已读 | 同上 |
| S2-4 | `bash` 工具：`asyncio.create_subprocess_exec`、超时、输出上限、取消传播 | 取消时子进程真的被杀（无僵尸） |
| S2-5 | `grep` / `glob` 工具，标记 `concurrency_safe` | 与 read 并发执行 |
| S2-6 | `permission/`：模式 + deny/allow/ask 规则 + 输入模式匹配器 | 判定顺序六步各有测试 |
| S2-7 | 会话授权表：`allow_always` 持久化到 session log | 授权后同类调用不再询问；续跑后授权仍在 |
| S2-8 | 权限判定全部写入 `permission` 日志记录 | 可从日志重建"每次为什么放行/拒绝" |
| S2-9 | CLI 权限确认 UI：执行前弹确认，支持 allow / deny / always | 交互可用 |
| S2-10 | `sandbox/`：process 后端（cwd 限制、路径逃逸检查、网络开关） | `..`、符号链接、绝对路径逃逸全部拦截 |

**S2 出口标准**：给它一个真实的小仓库，让它"修复这个测试"，它能读代码、改文件、跑 pytest、报告结果；每个写操作都经过确认；全程可 Ctrl-C 打断且会话不坏。

---

## S3 · 长任务不炸

**目标**：200 轮的长任务能跑完，缓存命中率可测。

| ID | 任务 | 验收 |
|---|---|---|
| S3-1 | 工具结果截断 + 溢出落盘 + 路径提示 | 单个结果超阈值时上下文里只留摘要 + 路径 |
| S3-2 | token 预算：每轮估算，接近阈值触发压缩 | 预算检查不产生网络往返 |
| S3-3 | `Compactor` 协议 + 服务端压缩委托实现 | capability 支持时走服务端 |
| S3-4 | 自研压缩实现：保留原始用户意图、最近文件编辑、当前任务状态 | 压缩后仍能正确继续中断的任务（回归用例） |
| S3-5 | `stable_prefix` 标记：上下文层在语义边界打标 | 标记数量不受厂商配额影响 |
| S3-6 | **Anthropic 断点选择算法**（适配器内）：turn 边界优先、长 turn 每 ~15 block 补一个、上限 4 个淘汰最旧 | 30+ block 的长 turn 下 `cache_read_input_tokens` 非零 |
| S3-7 | 缓存命中率可观测：usage 三个字段进 Event 与日志 | 能画出一次会话的命中率曲线 |
| S3-8 | 会话中途 system 消息注入（`inline_system` 能力），不支持时降级 | 注入后缓存前缀不失效（命中率断言） |

**S3 出口标准**：一个跑 200+ 轮、上下文超过窗口的真实任务能完整跑完；`cache_read_input_tokens` 在长会话中稳定非零。

---

## S4 · 扩展

| ID | 任务 | 验收 |
|---|---|---|
| S4-1 | `providers/openai/` | 通过契约测试套件 |
| S4-2 | `providers/openai_compatible/`，能力逐 endpoint 配置 | 对至少两个真实端点（如 GLM、Ollama）打通 |
| S4-3 | 跨 provider 切换：`ThinkingBlock` 按 `provider` 丢弃 | 会话中途换 provider 不报错 |
| S4-4 | `hooks/`：生命周期分发 | 一个 hook 能拦截工具调用 |
| S4-5 | skills：`.md` 按需加载 | 索引进 system prompt，正文按需读 |
| S4-6 | subagent 作为一个 Tool：独立 context、工具子集、深度限制 | 父 agent 只看到最终文本 |
| S4-7 | MCP 客户端 | 接一个真实 MCP server |
| S4-8 | 项目记忆（`CLAUDE.md` 等价物）加载 | 进入 system prompt 的稳定前缀 |

---

## S5 · 产品层

| ID | 任务 |
|---|---|
| S5-1 | TUI（技术选型待定：Textual / prompt_toolkit / Ink） |
| S5-2 | slash commands |
| S5-3 | plan 模式 UI |
| S5-4 | 会话浏览 / fork / rewind 的交互 |
| S5-5 | 观测导出（trace / OTel） |

---

## 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 中断修复写错 → 会话永久 400 | 致命，用户数据损失 | S1-16 单测覆盖二次取消；修复逻辑 `asyncio.shield` |
| 断点策略静默失效（20-block 窗口） | 账单翻倍，无报错 | S3-7 命中率可观测，加回归断言 |
| 编辑工具盲写覆盖用户改动 | 用户信任崩塌 | S2-1 file state tracker 是 L0 组件，不是工具私事 |
| bash 取消留僵尸进程 | 资源泄漏 | S2-4 显式进程组管理 + 测试断言 |
| 内核被厂商术语污染 | 多 provider 变成假的 | S0-4 依赖守卫 + S1-9 契约测试 |
| 范围蔓延到通用编排框架 | 什么都做不好 | ARCHITECTURE §1 非目标清单 |
