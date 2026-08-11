# AIHI

AIHI 是面向多种 Agent 产品的可复用基础设施 monorepo，发布两个 PEP 420 namespace 包：

- `aihi-models`（`aihi.models`）：模型消息契约、流式 Provider Protocol、版本化 Message codec，
  以及 Fake/OpenAI/Anthropic/OpenAI-compatible/DeepSeek Provider；
- `aihi-agent`（`aihi.agent`）：可恢复 Agent Loop、Event/Session、Context/Compaction、Tool、
  Policy/Approval、Sandbox、Memory、Skill、Subagent、Plugin/MCP、Observability 和 Eval。

依赖始终单向：

```text
aihi.models ← aihi.agent ← application
```

Router、Gateway、模型角色、产品 Prompt、默认模型、默认工具集和交互界面属于应用层。本仓库当前
不包含 `aihi-code-agent`；基础包完成后再单独确认应用层。

## 核心不变式

1. Event Log 是事实源，模型不是；Compaction 不覆盖原始 Event。
2. 所有副作用经过 `tools → policy → hooks → sandbox`。
3. Policy 返回 `ASK` 时 Run 挂起并可恢复，不自动批准或伪造 Tool Result。
4. Assistant Tool Call 先持久化再执行，每个调用最终恰有一个结果。
5. Host 不是隔离边界，必须显式设置 `unsafe=True`。

## 快速开始

```python
from aihi.agent import HostBackend, ReadFileTool, RuntimeBuilder
from aihi.models import FakeProvider, FakeStep

runtime = RuntimeBuilder(
    provider=FakeProvider([FakeStep(text="done")]),
    model="fake-model",
    sandbox=HostBackend(".", unsafe=True),
    tools=[ReadFileTool()],
).build()
```

`provider`、`model`、`sandbox` 和 `tools` 都没有默认值。应用只能把 `aihi.models.__all__` 与
`aihi.agent.__all__` 作为受支持的组合面；子模块路径属于内部实现。

## 安装与开发

```bash
python3 -m pip install -e packages/aihi/models -e packages/aihi/agent
python3 -m compileall -q packages
python3 -m pytest
ruff check .
mypy
```

## 文档

- [开发规范](AGENTS.md)
- [架构设计](docs/ARCHITECTURE.md)
- [任务分解](docs/TASK.md)
- [ADR-0030：AIHI 多包边界](docs/adr/0030-aihi-multi-package-boundary.md)
