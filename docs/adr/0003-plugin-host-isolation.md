# ADR-0003：第三方插件使用独立 Plugin Host

- 状态：Accepted
- 日期：2026-08-04

## 决策

第三方 Plugin 不允许直接 import 进入 Harness 主进程。`plugins/` 负责 Manifest、版本、
Hash、发现、信任和启动；Plugin Host 以子进程运行，通过版本化 JSON-RPC 暴露 Tool、Skill、
Hook 和 Agent 能力。

项目级插件默认关闭。`plugin.json` 只允许声明版本、Harness API 约束、能力、权限和可选
Entry Point；Discovery 只读 Manifest 和内容 Hash，不 import 或执行插件代码。启用后需要对
精确的版本、Manifest Hash 和内容 Hash 显式信任并记录原子更新的 lockfile；插件 Tool 仍必须经过统一的
`tools → policy → hooks → sandbox` 链路。Plugin Host 的能力集合只能是当前 Run 的子集。
Plugin Host 激活前必须重新 Discovery/Hash 校验，不能直接使用旧的候选路径快照。

## 原因

动态加载第三方 Python 代码会把插件故障、依赖污染和任意代码执行直接带入主 Runtime。独立
Host 能隔离崩溃和依赖，同时保留渐进式扩展能力。
