# ADR-0010：Docker、Worktree 与 Patch Artifact 边界

- 状态：Accepted
- 日期：2026-08-06
- 关联：`docs/ARCHITECTURE.md`、`docs/TASK.md`、ADR-0008、ADR-0009

## 决策

1. `DockerBackend` 是可选的强隔离执行后端。命令必须通过 argv 传递给 Docker CLI，默认关闭
   网络、只读容器根、`no-new-privileges`、丢弃 Linux capabilities、独立 `/tmp`，并设置 PID、
   内存和 CPU 上限；workspace 是唯一 bind mount。Docker 不可用或 daemon 不可用时 fail closed，
   不得自动改用 Host 或 Local。
2. Docker 后端的 `SandboxDescriptor` 声明 `filesystem_isolated=true`、`network_isolated`、
   `process_isolated`、`filesystem_write_isolated`、`mechanism=docker`、image、network mode 和
   `/workspace` mount scope。Policy 的完整隔离 Profile 只接受该能力，不根据名称猜测；运行事件
   可据此回放实际执行边界。
3. `WorktreeSpec` 只保存子任务拥有的 canonical root、base commit、只读标记和 allowed paths；
   它不是 Git 操作器，也不是独立安全边界。`PatchArtifact` 只保存外置 Diff Artifact 引用、base
   commit、变更路径和 SHA-256，不把大型 Diff 塞入 Event。
4. `WorktreePatchBoundary` 在未来合并前强制验证 task owner、base commit、路径 scope 和 `.git`
   禁止路径。本 ADR 不实现自动 Worktree 创建、Patch apply/merge 或冲突解决。

## 原因

Docker 适合需要完整文件系统和进程边界的任务；Worktree/Patch 类型则先固定子代理产物的归属和
可审计性，避免把 Git 命令副作用混入 Runtime，后续可以由受治理 Worker 实现。
