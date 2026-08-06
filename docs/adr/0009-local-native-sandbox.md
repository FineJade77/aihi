# ADR-0009：OS-native Local Isolated Sandbox

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0001、ADR-0008

## 决策

除显式 `unsafe=true` 的 `HostBackend` 和可选 Docker 外，提供独立的
`LocalIsolatedBackend`：

1. Linux 通过 bubblewrap namespace，macOS 通过 Seatbelt launcher；平台探测不到 launcher 时
   构造直接失败，不能静默回退到 Host。
2. launcher 以能力协议注入，必须声明 workspace 外写约束、网络隔离和进程隔离能力。后端将
   `filesystem_write_isolated`、`filesystem_isolated`、`network_isolated`、`process_isolated` 和
   `mechanism` 写入 `SandboxDescriptor`，策略以 descriptor 为事实，不根据后端名称猜测能力。
3. 本地后端默认关闭网络，并将 workspace 外路径保持只读；workspace 内文件仍经 canonical path、
   symlink escape、原子写入、摘要校验和输出/超时限制。完整文件系统可见性/机密隔离不由该后端
   声称，要求该能力的 Profile 必须选择 `filesystem_isolated=true` 的后端。
4. `unsafe` 对 Local backend 固定为 `false`，但这不代表所有平台都提供相同强度的隔离；事件
   必须记录 descriptor 的能力和 mechanism。

## 原因

本地 native sandbox 启动快、依赖少，适合开发机和受控 CI；把 launcher 能力显式化可以避免把
workspace 路径约束或 Git Worktree 误认为安全边界。Docker、gVisor 或 Firecracker 仍用于更强的
文件系统、进程和资源隔离。
