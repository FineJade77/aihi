# ADR-0034：Code Protocol 0.2 与完整 Session Replay

- 状态：Accepted
- 日期：2026-08-12
- 关联：[ADR-0033](0033-code-agent-cli-bridge.md)、[RFC-0003](../rfcs/0003-code-agent-domain-architecture.md)

## 背景

`run.start` / `run.resume` 已从长时间阻塞请求演进为立即确认，但 0.1 类型仍允许
`run_id = null`；后台 Run 在产生 canonical Run Event 前失败时使用的 `run.error` 也没有进入共享
Schema，并且缺少 `session_id`，多 Session 客户端无法可靠归属错误。Approval 展示所需的
Tool input preview、capabilities 与 sandbox 字段同样只存在于实现，没有冻结为协议。

CLI 恢复 Session 时只读取首个 100 Event。Session 超过该长度后，最后一条 Assistant Message、
历史序号和恢复界面都可能来自旧页面，而 Event Store 已经提供 `after_seq` / `has_more` 分页契约。

## 决策

本地 Worker 协议升级为 **0.2**，采用 exact-version handshake，不在同一连接中协商 0.1：

- `run.start` / `run.resume` 成功响应固定为 `{run_id: string, accepted: true}`；Run 进度和终态只以
  `event` notification 为事实源。
- 已确认但尚未产生 canonical Run Event 的失败使用 `run.error`，必须包含
  `protocol_version + session_id + run_id + message`。
- Approval descriptor 正式包含经过限长、凭据脱敏的 `tool_input` preview，以及
  `required_capabilities`、`reason` 和 `sandbox`；preview 只能展示，禁止作为 Tool 执行输入。
- `@aihi/code-protocol` 集中维护完整 RPC method map、DTO 与关键边界的 runtime guards；Client
  不再复制请求参数类型，也不以无校验 type assertion 接收 Run/Event notification。
- Session 恢复必须循环读取 `session.events`，直到 `has_more = false` 且 cursor 追平该页
  `head_seq`；Event 必须属于目标 Session 且 seq 连续递增。允许为并发落盘造成的 tail race 做一次
  空页重读，其余 cursor 停滞一律 fail closed，禁止静默展示截断历史。

JSON-RPC envelope 和 `Content-Length` framing 不变；0.2 仍是本地子进程 transport，不引入 Socket、
HTTP 或远程 Worker。

## 理由

这是一次已有行为的契约化，而不是增加第二条执行路径。强制版本一致比宽松兼容安全：0.1 Client
无法理解 `run.error` 的 Session 归属，也不能依赖非空 Run ID，继续连接只会制造“UI 认为失败、
Worker 仍在运行”的分叉状态。完整 replay 则让 Event Log 真正成为 TUI 重连后的事实源。

## 备选方案

- **0.1 additive 扩展**：拒绝。`run_id` 从 nullable 到 required 是语义收紧，伪装为 additive 会让
  老 Client 接受自己不能完整处理的消息。
- **只修 TUI，不冻结 Schema**：拒绝。下一种前端仍会复制同一批 DTO 和错误。
- **把终态放回 RPC Response**：拒绝。重新引入长请求 timeout，并与事件流形成两个事实源。

## 后果

- CLI 与 Worker 必须成对升级；不匹配时在 initialize 阶段明确失败。
- 长 Session 恢复会进行多次分页请求，但每次最多 500 Event，且不会把一次无限制查询推给
  Worker。
- 未来新增 notification 或改变 Run acknowledgement 时必须再次升级协议版本或证明为真正的
  additive 变更，并同步 TypeScript guard 与 JSON Schema。
