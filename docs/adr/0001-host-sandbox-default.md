# ADR-0001：Host 为首选执行后端，但必须显式 unsafe

- 状态：Accepted
- 日期：2026-08-04

## 背景

本地 Coding Agent 的目标环境不一定安装 Docker 或其他隔离运行时。Host 执行可以降低安装
门槛，但不能提供可信的文件、网络和进程隔离。

## 决策

`HostBackend` 是本地首选，但构造和运行选项都必须显式声明 `unsafe=true`。缺失声明时，
即使工具是只读的也拒绝执行。每个 Run 和 Tool Execution 都记录：

```json
{
  "sandbox": "host",
  "unsafe": true,
  "filesystem_isolated": false,
  "network_isolated": false
}
```

Host 仍提供 workspace canonical path、symlink escape 检查、超时、输出上限和进程组清理；
这些是风险降低措施，不是安全边界。`DockerBackend` 是可选后端；`require_isolation=true`
的策略 Profile 必须拒绝 Host，包括显式 unsafe 的 Host。

## 后果

- 本地启动不依赖外部运行时；
- 用户和审计系统明确看到 Host 风险；
- 需要真实隔离的部署必须使用 Docker 或后续远程执行后端。
