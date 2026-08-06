# aicode

`aicode` 是 Coding Agent 应用层，直接复用 `aiharness` 的 Provider、Tool、Policy、Sandbox、
Runtime 和 Session 实现。应用层只负责配置、Coding Tool 组合、CLI 和后续项目上下文工作流，
不复制 Harness 代码。

所有依赖只能来自 `from aiharness import ...`；深层子模块导入由
`tests/test_import_boundary.py` 阻断。

## 当前骨架

- `src/aicode/config.py`：Provider、模型、workspace、数据库和 Host unsafe 配置；
- `src/aicode/app.py`：组装现有 Harness 能力的 `build_runtime`；
- `src/aicode/approvals.py`：终端 Approval UX（Harness 只定义 Resolver 契约）；
- `src/aicode/cli.py`：独立 `aicode` CLI，支持 Fake/真实 Provider 配置和持久化 Session；
- `tests/`：应用层组合契约测试。

## Approval 流程

需要批准的工具不会被伪造成失败，Run 会挂起（退出码 `2`）：

```bash
aicode run "重构这个模块" --unsafe-host          # 挂起并打印 approval_id / run_id
aicode approve <session> <approval_id>          # 或 --deny
aicode resume <session> --run <run_id> --unsafe-host
```

在终端内直接回答：

```bash
aicode run "重构这个模块" --unsafe-host -i       # y=批准 n=拒绝 其他=挂起
```

`--accept-edits` 只自动放行工作区编辑；`shell` / `run_tests` 这类执行进程的工具
仍然逐次需要批准。批准一次后，该工具在当前 Run 内不再询问。

`AGENTS.md`/Skill 注入、Memory 和真实 Subagent 工作流仍属于后续 H-02 与应用层任务。

在仓库根目录进行本地验证：

```bash
PYTHONPATH=src:aicode/src python3 -m pytest aicode/tests
PYTHONPATH=src:aicode/src python3 -m aicode --help
```
