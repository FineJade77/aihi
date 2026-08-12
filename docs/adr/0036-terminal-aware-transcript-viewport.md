# ADR-0036：终端感知的 Transcript Viewport 与 Composer

- 状态：Accepted
- 日期：2026-08-12
- 关联：[ADR-0035](0035-event-driven-cli-transcript.md)

## 背景

事件驱动 Transcript 已能统一 replay 与实时展示，但 TUI 固定截取最后 12 个条目，长消息、Tool
输出和窄终端都会突破可用高度；用户也不能回看较早内容。原单行输入组件不支持多行草稿、命令历史
或 slash 补全，继续把这些状态直接堆入 `app.tsx` 会让键盘路由难以测试。

## 决策

在 `aihi-code-cli` 内建立两个可丢弃的应用层状态模型：

- viewport 先把 Transcript entry 按当前终端列宽转换为显示行，再按行预算选择窗口；默认跟随尾部，
  `PageUp` / `PageDown` 暂停并移动窗口，`Ctrl-E` 恢复跟随；
- Tool detail 默认折叠，`Ctrl-O` 统一展开或折叠；该状态不修改 Transcript entry；
- Approval 面板与 Transcript Tool preview 同样只显示白名单摘要，不通用序列化 Tool input；
- composer 保存光标、最多 100 条进程内历史、最多 32,000 字符草稿和 slash completion cycle；
  `Ctrl-J` 插入换行，`Enter` 提交完整草稿，`Tab` / `Shift-Tab` 完成命令；
- slash command descriptor 是帮助文本、候选列表与 completion 的单一目录；
- viewport 与 composer transition 使用纯函数测试，Ink adapter 再用真实输入流覆盖键盘解析；
- Approval 等待期间只有无修饰的 `y` / `o` / `n` 能授权，Ctrl/Meta 快捷键不能落入审批选择。

这些状态都不新增 Worker command、不改变 Code Protocol 0.2 或 Event Schema。Composer 历史只在当前
进程存在；提交后的用户消息仍只能由 Worker 写入 Event Store。

## 后果

- 终端 resize、长消息和多行输入不会再依赖固定条目数；
- 用户向上滚动后实时事件仍进入同一 Transcript，但视图保持暂停并提示存在更新，直到向下或跟随；
- Tool output 折叠减少屏幕噪声，完整结果仍保留在 Transcript 投影；
- Session/Provider/Model 的搜索式选择、`/status` 与 `/doctor` 在 P-01.4 中作为 CLI 应用层能力补齐，继续复用既有 RPC，不改变协议。
