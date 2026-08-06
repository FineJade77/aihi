# ADR-0005：可治理的 Hook 生命周期总线

- 状态：Accepted
- 日期：2026-08-06

## 决策

Hook 通过 `HookBus` 注册到明确的生命周期事件。注册记录包含稳定 ID、来源、优先级、超时、
失败策略和是否有副作用；同一事件按优先级升序、注册序号升序执行。每次调用都使用独立的
事件快照，防止某个 Hook 修改后续 Hook 或 Runtime 的输入。

只读 Hook 可观察事件；有副作用的 Hook 必须显式 Trust，并由调用方提供 `HookGovernance`，
其中包含已通过 Policy 的决定和 Sandbox 描述，必要时绑定 Approval 与 Capability Lease。
HookBus 不生成治理证据，也不提供绕过 Policy、Approval 或 Sandbox 的 API。

单个 Hook 超时会取消其协程并产生稳定的 `hook_timeout` 结果。`fail_fast` 停止后续 Hook 并
返回 `hook_dispatch_failed`；`continue` 记录错误后继续。每次 Dispatch 返回逐 Hook 的结果，
供 Runtime、审计和可观测性使用。

## 原因

Hook 是跨 Runtime 的扩展点，必须具备确定性、可取消性和失败可见性。将治理证据作为调用方
输入而不是 Hook 自行声明，可以防止扩展代码把观察能力伪装成执行权限。
