# ADR-0007：Plugin Host 激活边界与进程生命周期

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0003

## 决策

Plugin Host 激活分为四个不可跳过的阶段：

1. 使用独立 `PluginDiscovery` 重新读取 Manifest 并计算内容 Hash；
2. `PluginTrustManager` 要求精确的 `plugin_id@version + manifest_sha256 + content_sha256` Trust
   记录和 enabled 状态；
3. `PluginHostPolicy` 检查 Manifest 的 `capabilities`、`permissions` 是否分别是当前 Run 显式
   allowlist 的子集；
4. 用最小环境、`shell=False`、独立进程组启动 `aihi.agent.plugins.host_worker`，通过
   兼容标识 `aiharness.plugin.v1` 的 JSON-lines JSON-RPC 暴露有限的 Tool/Skill/Hook 方法。

主进程不 import Plugin Entry Point。Tool 通过 `PluginRemoteTool` 适配到 `ToolRegistry`，所以
输入校验、Policy、Hook 和 Sandbox 仍由统一 Dispatcher 处理。Plugin Host 不获得或授予新的
Approval、Capability Lease 或 Sandbox 权限。

Host 对请求/响应设置大小和时间上限；协议错误、EOF、超时或启动失败均为稳定错误，并清理
整个进程组。停止时先发送有界 `shutdown`，再发送终止信号，最后使用强制终止兜底。可能已经
产生副作用的 Tool 不在 Host 边界自动重放。

## 原因

独立进程避免第三方依赖、崩溃和模块副作用进入 Runtime 主进程，同时保留插件的渐进扩展能力。
在 Host 之外再次执行 Trust 和策略校验可缩短 TOCTOU 窗口，并防止仅凭旧的候选对象启动插件。
