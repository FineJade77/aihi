# aicode

`aicode` 是 Coding Agent 应用层，直接复用 `aiharness` 的 Provider、Tool、Policy、Sandbox、
Runtime 和 Session 实现。应用层只负责配置、Coding Tool 组合、CLI 和后续项目上下文工作流，
不复制 Harness 代码。

## 当前骨架

- `src/aicode/config.py`：Provider、模型、workspace、数据库和 Host unsafe 配置；
- `src/aicode/app.py`：组装现有 Harness 能力的 `build_runtime`；
- `src/aicode/cli.py`：独立 `aicode` CLI，支持 Fake/真实 Provider 配置和持久化 Session；
- `tests/`：应用层组合契约测试。

当前 `--accept-edits` 是显式的本地开发开关；交互式 Approval/Resume、`AGENTS.md`/Skill 注入、
Memory 和真实 Subagent 工作流属于后续 H-02 与应用层任务。

在仓库根目录进行本地验证：

```bash
PYTHONPATH=src:aicode/src python3 -m pytest aicode/tests
PYTHONPATH=src:aicode/src python3 -m aicode --help
```
