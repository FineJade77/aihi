# Coding Agent 领域架构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `aihi.code_agent` 补上领域层——类型化流式 Turn 事件、声明式工具集、分层 coding 提示词、内置 Skill、命名 Subagent 类型。

**Architecture:** 领域能力收回包内，`runtime.py` 收敛为纯拼装。`stream()` 用「单一常驻 Session 观察者 + 队列哨兵」保证 `TurnFinished` 之前事件已排空。`aihi.agent` 仅扩展 `SubagentTool` 接受命名 Runner 映射，任务治理不下放应用层。

**Tech Stack:** Python 3.11、hatchling、pytest（`asyncio_mode=auto`）、ruff（line-length 100）、mypy strict。

## Global Constraints

- 依赖方向不可逆：`aihi.models ← aihi.agent ← aihi.code_agent`。
- ruff line-length 100，target py311，lint 选择 `E,F,I,UP,B`。
- mypy strict 必须通过：`python3 -m mypy`。
- 全量测试必须通过：`python3 -m pytest`（当前基线 360 passed）。
- 本轮不改 Worker 对外协议，不改 `apps/aihi-code-cli`。
- 新增包数据（`.md`）必须同步登记到 `pyproject.toml` 的 `artifacts`，否则不进 wheel。
- 提交信息用一行祈使句，不加任何尾注。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `code_agent/turns.py` | Turn 事件模型、事件映射、`stream()` |
| `code_agent/tools/registry.py` | `ToolDefinition` / `ToolBuildContext` |
| `code_agent/tools/__init__.py` | `CODING_TOOLSET`、`build_tools()` |
| `code_agent/tools/git.py` | 由 `coding_tools.py` 迁入 |
| `code_agent/tools/skill.py` | 由 `skills.py` 迁入 |
| `code_agent/prompts/__init__.py` | `compose_system_prompt()` |
| `code_agent/prompts/coding.md` | 内置 coding 提示词 |
| `code_agent/skills/__init__.py` | `builtin_skill_root()` |
| `code_agent/skills/builtin/*.md` | 内置 Skill 正文 |
| `code_agent/subagents/registry.py` | `SubagentDefinition` |
| `code_agent/subagents/__init__.py` | `CODING_SUBAGENTS`、`build_subagent_runners()` |
| `code_agent/subagents/prompts/*.md` | 各 Subagent 类型提示词 |
| `agent/agents/subagent.py` | `SubagentTool` 接受命名 Runner 映射 |
| `agent/builder.py` | `with_subagents(runners=...)` |
| `code_agent/runtime.py` | 仅拼装 |
| `code_agent/config.py` | `system_prompt_mode`、`subagents.types` |

---

### Task 1: Turn 事件模型与流式入口

**Files:**
- Create: `packages/aihi/code-agent/src/aihi/code_agent/turns.py`
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/runtime.py`（`run()` 改为消费 `stream()`）
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/__init__.py`（导出事件类型与 `stream`）
- Test: `packages/aihi/code-agent/tests/test_turns.py`

**Interfaces:**
- Consumes: `CodeAgentRuntime.create(config, store=None)`（既有）、`aihi.agent.Event`、`aihi.agent.runtime.RunResult`
- Produces:
  - `TurnEvent(seq: int | None, run_id: str | None)` 基类，全部 `kw_only=True`
  - `TextDelta(text: str)`、`AssistantMessage(text: str, data: dict[str, Any])`
  - `ToolCallStarted(call_id, tool_name, input)`、`ToolCallFinished(call_id, tool_name, is_error)`
  - `ApprovalRequested(approval_id, tool_name: str | None, scope: str)`
  - `RunStateChanged(state: str)`
  - `SubagentSpawned(task_id, objective)`、`SubagentStarted(task_id)`、`SubagentCompleted(task_id, state)`
  - `TurnFinished(result: RunResult)`
  - `CodeAgentRuntime.stream(session, *, user_message, run_id=None, model=None, system_prompt=None, max_output_tokens=None, cancel_event=None) -> AsyncIterator[TurnEvent]`

- [ ] **Step 1: 写失败测试**

创建 `packages/aihi/code-agent/tests/test_turns.py`：

```python
from __future__ import annotations

import pytest
from aihi.agent import InMemoryEventStore, Session
from aihi.code_agent.config import load_config
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.turns import AssistantMessage, TextDelta, TurnFinished


def _config(tmp_path):
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n',
        encoding="utf-8",
    )
    return load_config(path, cwd=tmp_path)


async def test_stream_ends_with_turn_finished_after_draining_events(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        events = [
            event async for event in runtime.stream(session, user_message="hi")
        ]
    finally:
        await runtime.close()

    assert isinstance(events[-1], TurnFinished)
    assert events[-1].result.state == "completed"
    # The whole point of the ordering invariant: nothing arrives after the end.
    assert not any(isinstance(event, TurnFinished) for event in events[:-1])
    assert any(isinstance(event, TextDelta) for event in events)
    assert any(isinstance(event, AssistantMessage) for event in events)


async def test_stream_rejects_an_empty_user_message(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        with pytest.raises(ValueError):
            async for _ in runtime.stream(session, user_message="   "):
                pass
    finally:
        await runtime.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_turns.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'aihi.code_agent.turns'`

- [ ] **Step 3: 写 `turns.py`**

```python
"""Typed Turn events and the streaming entry point for one user turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from aihi.agent import Event
from aihi.agent.runtime import RunResult


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEvent:
    seq: int | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDelta(TurnEvent):
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage(TurnEvent):
    text: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallStarted(TurnEvent):
    call_id: str
    tool_name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallFinished(TurnEvent):
    call_id: str
    tool_name: str
    is_error: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(TurnEvent):
    approval_id: str
    tool_name: str | None
    scope: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStateChanged(TurnEvent):
    state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentSpawned(TurnEvent):
    task_id: str
    objective: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentStarted(TurnEvent):
    task_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentCompleted(TurnEvent):
    task_id: str
    state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFinished(TurnEvent):
    result: RunResult


_DONE = object()


class TurnEventPump:
    """One long-lived Session observer routing events into the active turn.

    ``Session`` offers ``add_event_observer`` but no removal, and de-duplicates
    by identity. A stable bound method is therefore installed once per Session
    and switched off by clearing the queue rather than by detaching.
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] | None = None

    def attach(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    def detach(self) -> None:
        self._queue = None

    def observe(self, event: Event) -> None:
        queue = self._queue
        if queue is not None:
            queue.put_nowait(event)


def message_text(data: dict[str, Any]) -> str:
    """Join the text parts of a Message payload; other content kinds are skipped."""

    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("kind") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def map_event(event: Event) -> TurnEvent | None:
    """Translate one canonical Event into a domain Turn event, or drop it."""

    data = event.data
    seq = event.seq
    run_id = event.run_id
    if event.type == "model.chunk":
        if data.get("kind") != "text_delta":
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        return TextDelta(seq=seq, run_id=run_id, text=text)
    if event.type == "assistant.message":
        return AssistantMessage(
            seq=seq, run_id=run_id, text=message_text(data), data=dict(data)
        )
    if event.type == "tool.call.started":
        return ToolCallStarted(
            seq=seq,
            run_id=run_id,
            call_id=str(data.get("tool_call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            input=dict(data.get("input", {})),
        )
    if event.type == "tool.call.completed":
        return ToolCallFinished(
            seq=seq,
            run_id=run_id,
            call_id=str(data.get("tool_call_id", "")),
            tool_name=str(data.get("tool_name", "")),
            is_error=bool(data.get("is_error", False)),
        )
    if event.type == "approval.requested":
        approval = data.get("approval")
        approval = approval if isinstance(approval, dict) else {}
        tool_name = data.get("tool_name")
        return ApprovalRequested(
            seq=seq,
            run_id=run_id,
            approval_id=str(approval.get("approval_id", "")),
            tool_name=tool_name if isinstance(tool_name, str) else None,
            scope=str(approval.get("scope", "")),
        )
    if event.type == "run.state_changed":
        return RunStateChanged(seq=seq, run_id=run_id, state=str(data.get("state", "")))
    if event.type == "subagent.spawned":
        return SubagentSpawned(
            seq=seq,
            run_id=run_id,
            task_id=str(data.get("task_id", "")),
            objective=str(data.get("objective", "")),
        )
    if event.type == "subagent.started":
        return SubagentStarted(seq=seq, run_id=run_id, task_id=str(data.get("task_id", "")))
    if event.type == "subagent.completed":
        return SubagentCompleted(
            seq=seq,
            run_id=run_id,
            task_id=str(data.get("task_id", "")),
            state=str(data.get("state", "")),
        )
    return None


async def drive_turn(
    *,
    session: Any,
    pump: TurnEventPump,
    invoke: Callable[[], Any],
) -> AsyncIterator[TurnEvent]:
    """Yield mapped events, then TurnFinished once the queue is drained.

    The sentinel is enqueued in the driver's ``finally``, and observers fire
    synchronously while the coordinator appends. Every event of this run is
    therefore already queued ahead of the sentinel — which is exactly the
    ordering guarantee consumers need.
    """

    queue: asyncio.Queue[Any] = asyncio.Queue()
    session.add_event_observer(pump.observe)
    pump.attach(queue)

    async def driver() -> RunResult:
        try:
            return await invoke()
        finally:
            queue.put_nowait(_DONE)

    task: asyncio.Task[RunResult] = asyncio.create_task(driver())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            mapped = map_event(item)
            if mapped is not None:
                yield mapped
        result = await task
        yield TurnFinished(seq=None, run_id=result.run_id, result=result)
    finally:
        pump.detach()
        if not task.done():
            task.cancel()


__all__ = [
    "ApprovalRequested",
    "AssistantMessage",
    "RunStateChanged",
    "SubagentCompleted",
    "SubagentSpawned",
    "SubagentStarted",
    "TextDelta",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnEvent",
    "TurnEventPump",
    "TurnFinished",
    "drive_turn",
    "map_event",
    "message_text",
]
```

- [ ] **Step 4: 在 `runtime.py` 接上 `stream()`，并让 `run()` 消费它**

在 `CodeAgentRuntime` 的 dataclass 字段中加入 `pump: TurnEventPump = field(default_factory=TurnEventPump)`（需 `from dataclasses import field`），并添加：

```python
    def stream(
        self,
        session: Session,
        *,
        user_message: str,
        run_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """Stream one user turn as typed domain events."""

        text = user_message.strip()
        if not text:
            raise CodeAgentConfigError("user_message must not be empty")

        def invoke() -> Any:
            return self.runtime.coordinator.run(
                session,
                model=model or self.config.provider.model,
                user_message=Message.text("user", text),
                run_id=run_id,
                permission_mode=self.config.permission_mode,
                require_capability_lease=self.config.require_capability_lease,
                system_prompt=(
                    self.config.system_prompt if system_prompt is None else system_prompt
                ),
                max_output_tokens=max_output_tokens or self.config.max_output_tokens,
                cancel_event=cancel_event,
            )

        return drive_turn(session=session, pump=self.pump, invoke=invoke)
```

把既有 `run()` 方法体替换为消费 `stream()`，避免两条并行路径：

```python
    async def run(
        self,
        session: Session,
        *,
        user_message: str,
        run_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> RunResult:
        """Run one user turn through the Harness coordinator loop."""

        final: RunResult | None = None
        async for event in self.stream(
            session,
            user_message=user_message,
            run_id=run_id,
            model=model,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            cancel_event=cancel_event,
        ):
            if isinstance(event, TurnFinished):
                final = event.result
        assert final is not None, "stream() must end with TurnFinished"
        return final
```

需要的新 import：`from collections.abc import AsyncIterator`、`from dataclasses import field`、`from typing import Any`，以及 `from .turns import TurnEvent, TurnEventPump, TurnFinished, drive_turn`。

`resume()` 本轮保持原样，不改。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_turns.py -v`
Expected: PASS，2 passed

- [ ] **Step 6: 确认既有测试与类型检查未回归**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 362 passed（原 360 + 新增 2）、ruff 通过、mypy 通过

若 `tool.call.started` / `tool.call.completed` 事件名与实际不符，用以下命令核对后修正 `map_event`，不要猜：

Run: `grep -rhno '"tool\.[a-z_.]*"' packages/aihi/agent/src | sort -u -t'"' -k2`

- [ ] **Step 7: 提交**

```bash
git add packages/aihi/code-agent/src/aihi/code_agent/turns.py \
        packages/aihi/code-agent/src/aihi/code_agent/runtime.py \
        packages/aihi/code-agent/src/aihi/code_agent/__init__.py \
        packages/aihi/code-agent/tests/test_turns.py
git commit -m "feat(code-agent): stream typed turn events from the domain layer"
```

---

### Task 2: 声明式工具集注册表

**Files:**
- Create: `packages/aihi/code-agent/src/aihi/code_agent/tools/__init__.py`
- Create: `packages/aihi/code-agent/src/aihi/code_agent/tools/registry.py`
- Create: `packages/aihi/code-agent/src/aihi/code_agent/tools/git.py`（内容由 `coding_tools.py` 迁入）
- Create: `packages/aihi/code-agent/src/aihi/code_agent/tools/skill.py`（内容由 `skills.py` 迁入）
- Delete: `packages/aihi/code-agent/src/aihi/code_agent/coding_tools.py`、`skills.py`
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/runtime.py`（删除 `_build_tools`）
- Test: `packages/aihi/code-agent/tests/test_tools_registry.py`

**Interfaces:**
- Consumes: Task 1 无依赖
- Produces:
  - `ToolBuildContext(config: CodeAgentConfig, skill_loader: SkillLoader | None)`
  - `ToolDefinition(name: str, factory: Callable[[ToolBuildContext], Tool], default_enabled: bool = True, requires: tuple[str, ...] = ())`
  - `CODING_TOOLSET: tuple[ToolDefinition, ...]`
  - `build_tools(context: ToolBuildContext) -> tuple[Tool, ...]`

- [ ] **Step 1: 写失败测试**

创建 `packages/aihi/code-agent/tests/test_tools_registry.py`：

```python
from __future__ import annotations

from aihi.code_agent.config import load_config
from aihi.code_agent.tools import CODING_TOOLSET, ToolBuildContext, build_tools


def test_toolset_names_are_unique_and_cover_the_config_default(tmp_path) -> None:
    names = [definition.name for definition in CODING_TOOLSET]
    assert len(names) == len(set(names))
    config = load_config(cwd=tmp_path)
    assert set(config.tools).issubset(set(names))


def test_build_tools_honours_the_config_allowlist(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\ntools = ["read_file", "grep"]\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    tools = build_tools(ToolBuildContext(config=config, skill_loader=None))
    assert sorted(tool.spec.name for tool in tools) == ["grep", "read_file"]


def test_tools_requiring_a_skill_loader_are_dropped_without_one(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\ntools = ["read_file", "load_skill"]\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    tools = build_tools(ToolBuildContext(config=config, skill_loader=None))
    assert [tool.spec.name for tool in tools] == ["read_file"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_tools_registry.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'aihi.code_agent.tools'`

- [ ] **Step 3: 写 `tools/registry.py`**

```python
"""Declarative definition of the Coding Agent tool set."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aihi.agent import SkillLoader, Tool

from ..config import CodeAgentConfig


@dataclass(frozen=True, slots=True)
class ToolBuildContext:
    """Everything a tool factory may need in order to construct its tool."""

    config: CodeAgentConfig
    skill_loader: SkillLoader | None = None

    def has(self, requirement: str) -> bool:
        return getattr(self, requirement, None) is not None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool the Coding Agent can offer, and how to construct it."""

    name: str
    factory: Callable[[ToolBuildContext], Tool]
    default_enabled: bool = True
    requires: tuple[str, ...] = ()

    def available(self, context: ToolBuildContext) -> bool:
        return all(context.has(requirement) for requirement in self.requires)


__all__ = ["ToolBuildContext", "ToolDefinition"]
```

- [ ] **Step 4: 迁移 `git.py` 与 `skill.py`**

```bash
git mv packages/aihi/code-agent/src/aihi/code_agent/coding_tools.py \
       packages/aihi/code-agent/src/aihi/code_agent/tools/git.py
git mv packages/aihi/code-agent/src/aihi/code_agent/skills.py \
       packages/aihi/code-agent/src/aihi/code_agent/tools/skill.py
```

两个文件内容不变，仅需把 `skill.py` 中 `from aihi.agent.skills import SkillLoader` 与
`from aihi.agent.tools import ...` 保持原样（它们是绝对导入，迁移后依然有效）。

- [ ] **Step 5: 写 `tools/__init__.py`**

```python
"""The Coding Agent tool set: one place that says which tools exist."""

from __future__ import annotations

from aihi.agent import (
    BashTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    Tool,
    WriteFileTool,
)

from .git import GitDiffTool, GitStatusTool
from .registry import ToolBuildContext, ToolDefinition
from .skill import LoadSkillTool


def _load_skill(context: ToolBuildContext) -> Tool:
    assert context.skill_loader is not None  # guarded by ToolDefinition.requires
    return LoadSkillTool(context.skill_loader)


CODING_TOOLSET: tuple[ToolDefinition, ...] = (
    ToolDefinition("read_file", lambda _: ReadFileTool()),
    ToolDefinition("glob", lambda _: GlobTool()),
    ToolDefinition("grep", lambda _: GrepTool()),
    ToolDefinition("git_status", lambda _: GitStatusTool()),
    ToolDefinition("git_diff", lambda _: GitDiffTool()),
    ToolDefinition("edit_file", lambda _: EditFileTool()),
    ToolDefinition("write_file", lambda _: WriteFileTool()),
    ToolDefinition("bash", lambda _: BashTool()),
    ToolDefinition("load_skill", _load_skill, requires=("skill_loader",)),
)


def build_tools(context: ToolBuildContext) -> tuple[Tool, ...]:
    """Build the configured tools, skipping any whose dependencies are absent."""

    allowed = set(context.config.tools)
    return tuple(
        definition.factory(context)
        for definition in CODING_TOOLSET
        if definition.name in allowed and definition.available(context)
    )


__all__ = [
    "CODING_TOOLSET",
    "GitDiffTool",
    "GitStatusTool",
    "LoadSkillTool",
    "ToolBuildContext",
    "ToolDefinition",
    "build_tools",
]
```

- [ ] **Step 6: 改 `runtime.py` 用注册表**

删除 `_build_tools` 函数，把 `RuntimeBuilder(... tools=_build_tools(config, skill_loader=skill_loader))` 改为：

```python
            tools=build_tools(ToolBuildContext(config=config, skill_loader=skill_loader)),
```

删除现已无用的工具 import（`BashTool`、`EditFileTool`、`GlobTool`、`GrepTool`、`ReadFileTool`、`WriteFileTool`、`GitDiffTool`、`GitStatusTool`、`LoadSkillTool`、`Tool`），改为 `from .tools import ToolBuildContext, build_tools`。

- [ ] **Step 7: 跑测试并确认无回归**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 全部通过（新增 3 个测试）

- [ ] **Step 8: 提交**

```bash
git add -A packages/aihi/code-agent
git commit -m "refactor(code-agent): declare the coding tool set in one registry"
```

---

### Task 3: 分层 coding system prompt

**Files:**
- Create: `packages/aihi/code-agent/src/aihi/code_agent/prompts/__init__.py`
- Create: `packages/aihi/code-agent/src/aihi/code_agent/prompts/coding.md`
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/config.py`（新增 `system_prompt_mode`）
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/runtime.py`（`stream()` 用组合结果）
- Test: `packages/aihi/code-agent/tests/test_prompts.py`

**Interfaces:**
- Consumes: Task 1 的 `CodeAgentRuntime.stream()`
- Produces:
  - `load_builtin_prompt() -> str`
  - `compose_system_prompt(config: CodeAgentConfig, *, workspace: Path) -> str`
  - `CodeAgentConfig.system_prompt_mode: str`（`"append"` | `"replace"`，默认 `"append"`）

- [ ] **Step 1: 写失败测试**

创建 `packages/aihi/code-agent/tests/test_prompts.py`：

```python
from __future__ import annotations

from aihi.code_agent.config import load_config
from aihi.code_agent.prompts import compose_system_prompt, load_builtin_prompt


def test_builtin_prompt_is_packaged_and_non_empty() -> None:
    prompt = load_builtin_prompt()
    assert "coding" in prompt.lower()
    assert len(prompt) > 200


def test_append_mode_keeps_the_builtin_prompt_and_adds_the_user_text(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt = "PROJECT RULE: always use tabs"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    composed = compose_system_prompt(config, workspace=tmp_path)
    assert load_builtin_prompt() in composed
    assert "PROJECT RULE: always use tabs" in composed
    assert composed.index(load_builtin_prompt()) < composed.index("PROJECT RULE")


def test_replace_mode_drops_the_builtin_prompt(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[agent]\nsystem_prompt = "ONLY THIS"\nsystem_prompt_mode = "replace"\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    composed = compose_system_prompt(config, workspace=tmp_path)
    assert composed.strip() == "ONLY THIS"


def test_workspace_conventions_are_included_when_present(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# House rules\nNo bare except.\n", encoding="utf-8")
    config = load_config(cwd=tmp_path)
    composed = compose_system_prompt(config, workspace=tmp_path)
    assert "No bare except." in composed


def test_environment_section_reports_the_workspace(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    composed = compose_system_prompt(config, workspace=tmp_path)
    assert str(tmp_path) in composed
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_prompts.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'aihi.code_agent.prompts'`

- [ ] **Step 3: 写 `prompts/coding.md`**

```markdown
You are a coding agent working directly in a user's repository.

## How you work

- Read before you write. Inspect the surrounding code and match its naming,
  structure, comment density, and error handling rather than importing your own
  conventions.
- Prefer the smallest change that fully solves the task. Do not bundle
  refactors, renames, or cleanups the user did not ask for.
- When a task is ambiguous in a way that changes the result, ask. When it is
  ambiguous in a way that does not, choose the conventional option and say which
  one you chose.
- Never invent APIs, flags, or file paths. Verify them by reading the code.

## Tools

- Search before editing: use `grep` and `glob` to locate code instead of
  guessing paths.
- `read_file` before `edit_file` on any file you have not already read.
- `bash` is for observation and verification. It acts on the user's real
  machine; prefer read-only commands and never run destructive ones unasked.
- `git_status` and `git_diff` are read-only and never stage or modify changes.

## Verification

- Run the project's own tests and linters after changing code, and report the
  actual output.
- If a check fails, say so plainly with the failure text. Never describe work as
  complete, passing, or fixed without having run the command that proves it.
- If you could not verify something, state that explicitly instead of implying
  success.

## Reporting

- Lead with what changed and what it means for the user, not with a narration of
  your steps.
- Reference code as `path/to/file.py:42` so it can be opened directly.
- Surface real problems you find, even when they are outside the requested
  scope — but do not fix them without being asked.
```

- [ ] **Step 4: 写 `prompts/__init__.py`**

```python
"""The layered system prompt for the Coding Agent."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from ..config import CodeAgentConfig

_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md")
_MAX_CONVENTION_CHARS = 8_000


def load_builtin_prompt() -> str:
    """Read the packaged coding prompt."""

    return (files(__package__) / "coding.md").read_text(encoding="utf-8").strip()


def _environment_section(config: CodeAgentConfig, workspace: Path) -> str:
    tools = ", ".join(config.tools) or "none"
    return (
        "## Environment\n\n"
        f"- workspace: {workspace}\n"
        f"- sandbox: {config.sandbox.backend}"
        f"{' (unsandboxed host access)' if config.sandbox.unsafe else ''}\n"
        f"- tools: {tools}"
    )


def _conventions_section(workspace: Path) -> str:
    for name in _CONVENTION_FILES:
        candidate = workspace / name
        if not candidate.is_file():
            continue
        try:
            body = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body:
            continue
        if len(body) > _MAX_CONVENTION_CHARS:
            body = f"{body[:_MAX_CONVENTION_CHARS]}\n…(truncated)"
        return f"## Project conventions ({name})\n\n{body}"
    return ""


def compose_system_prompt(config: CodeAgentConfig, *, workspace: Path) -> str:
    """Compose builtin prompt, environment, project conventions and user text."""

    if config.system_prompt_mode == "replace":
        return config.system_prompt
    sections = [
        load_builtin_prompt(),
        _environment_section(config, workspace),
        _conventions_section(workspace),
        config.system_prompt.strip(),
    ]
    return "\n\n".join(section for section in sections if section)


__all__ = ["compose_system_prompt", "load_builtin_prompt"]
```

- [ ] **Step 5: 在 `config.py` 增加 `system_prompt_mode`**

在 `CodeAgentConfig` 中于 `system_prompt: str = ""` 之后加入：

```python
    system_prompt_mode: str = "append"
```

在 `from_mapping` 中 `system_prompt = raw_system_prompt` 之后加入：

```python
        system_prompt_mode = _text(
            agent_map.get("system_prompt_mode", "append"), "agent.system_prompt_mode"
        ).lower()
        if system_prompt_mode not in {"append", "replace"}:
            raise CodeAgentConfigError(
                "agent.system_prompt_mode must be one of: append, replace"
            )
```

在该函数末尾的 `cls(...)` 构造中补上 `system_prompt_mode=system_prompt_mode`，并在
`public_descriptor()` 的 `"agent"` 段（若存在 `system_prompt` 相关字段处）补
`"system_prompt_mode": self.system_prompt_mode`。

- [ ] **Step 6: 在 `runtime.py` 使用组合提示词**

`stream()` 中的 `system_prompt=` 实参改为：

```python
                system_prompt=(
                    compose_system_prompt(self.config, workspace=Path(session.cwd))
                    if system_prompt is None
                    else system_prompt
                ),
```

新增 import：`from pathlib import Path`、`from .prompts import compose_system_prompt`。

- [ ] **Step 7: 跑测试确认通过并检查回归**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 全部通过

注意：若既有断言依赖空 system prompt，需按新语义更新该断言而非改回实现。

- [ ] **Step 8: 提交**

```bash
git add -A packages/aihi/code-agent
git commit -m "feat(code-agent): ship a layered coding system prompt"
```

---

### Task 4: 内置 Skill

**Files:**
- Create: `packages/aihi/code-agent/src/aihi/code_agent/skills/__init__.py`
- Create: `packages/aihi/code-agent/src/aihi/code_agent/skills/builtin/code_review.md`
- Create: `.../builtin/debug.md`、`.../builtin/test_writing.md`、`.../builtin/refactor.md`
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/runtime.py`（注入 BUILTIN 根、放宽锁文件检查）
- Test: `packages/aihi/code-agent/tests/test_builtin_skills.py`

**Interfaces:**
- Consumes: Task 2 的 `tools/skill.py`（`LoadSkillTool`）
- Produces: `builtin_skill_root() -> SkillRoot`、`BUILTIN_SKILL_NAMES: frozenset[str]`

- [ ] **Step 1: 确认 Skill markdown 的 frontmatter 格式**

内置 Skill 必须能被既有 `SkillDiscovery` 解析。先读一个既有测试夹具确认字段：

Run: `grep -rn "name:" -B 3 -A 6 packages/aihi/agent/tests | grep -i "skill" | head -20`
Run: `grep -n "frontmatter\|name\b.*version\|def parse" packages/aihi/agent/src/aihi/agent/skills/discovery.py | head -20`

用查到的真实字段名写下面的 markdown；**不要凭印象猜字段**。

- [ ] **Step 2: 写失败测试**

创建 `packages/aihi/code-agent/tests/test_builtin_skills.py`：

```python
from __future__ import annotations

from aihi.agent import SkillDiscovery, SkillScope
from aihi.code_agent.config import load_config
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.skills import builtin_skill_root


def test_builtin_root_is_discoverable_and_contains_code_review() -> None:
    root = builtin_skill_root()
    assert root.scope is SkillScope.BUILTIN
    names = {skill.name for skill in SkillDiscovery([root]).discover()}
    assert "code_review" in names


async def test_builtin_skills_need_no_trust_lockfile(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    assert config.skill_trust_path is None
    runtime = await CodeAgentRuntime.create(config)
    try:
        assert runtime.runtime.registry.get("load_skill") is not None
    finally:
        await runtime.close()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_builtin_skills.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'aihi.code_agent.skills'`

- [ ] **Step 4: 写 `skills/builtin/code_review.md`**

用 Step 1 查到的真实 frontmatter 字段，正文如下：

```markdown
Review changed code for defects that would actually bite, not for style.

## Order of work

1. Read the full diff first (`git_diff`). Do not review a file you have not read
   in its surrounding context.
2. For each change, ask what input or state would make it wrong. A finding you
   cannot turn into a concrete failure scenario is not a finding.
3. Check the callers of anything whose signature, return type, or error
   behaviour changed.

## What counts as a finding

- Correctness: wrong results, unhandled error paths, broken invariants,
  off-by-one, resource leaks, concurrency hazards.
- Security: injection, path traversal, secrets in code or logs, missing
  authorization on a state change.
- Regression risk: silent behaviour changes to an existing public interface.

## What does not

- Formatting a linter already enforces.
- Restating what the code does.
- Preferences with no defect behind them.

## Reporting

State each finding as: file:line, one sentence naming the defect, then the
concrete input or state that triggers it. Rank by severity. If nothing survives
that bar, say the diff looks correct and stop — do not pad the list.
```

`debug.md`、`test_writing.md`、`refactor.md` 同样结构：一段目的、有序步骤、判定标准、报告格式。

- [ ] **Step 5: 写 `skills/__init__.py`**

```python
"""Skills that ship with the Coding Agent distribution."""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from aihi.agent import SkillRoot, SkillScope

BUILTIN_SKILL_NAMES = frozenset({"code_review", "debug", "test_writing", "refactor"})


def builtin_skill_directory() -> Path:
    """Return the packaged builtin Skill directory on the filesystem."""

    resource = files(__package__) / "builtin"
    with as_file(resource) as path:
        return Path(path)


def builtin_skill_root() -> SkillRoot:
    """The BUILTIN Skill root, trusted because the package itself is trusted."""

    return SkillRoot(builtin_skill_directory(), SkillScope.BUILTIN)


__all__ = ["BUILTIN_SKILL_NAMES", "builtin_skill_directory", "builtin_skill_root"]
```

- [ ] **Step 6: 在 `runtime.py` 注入 BUILTIN 根并放宽锁文件检查**

把现有 skill 装配块替换为：

```python
        builtin_root = builtin_skill_root()
        configured_roots = [SkillRoot(root.path, root.scope) for root in config.skill_roots]
        skill_discovery = SkillDiscovery([builtin_root, *configured_roots])
        # Only non-BUILTIN scopes require explicit trust: a builtin Skill's
        # integrity is the package's integrity, and by the time it is read the
        # package's own code has already run.
        if configured_roots and config.skill_trust_path is None:
            raise CodeAgentConfigError("Skill roots require a trust lockfile path")
        trust_store = FileSkillTrustStore(
            config.skill_trust_path
            if config.skill_trust_path is not None
            else Path(config.base_dir) / ".aihi" / "skills.lock.json"
        )
        skill_loader = SkillLoader(
            SkillTrustManager(trust_store, discovery=skill_discovery),
            discovery=skill_discovery,
        )
```

新增 import：`from .skills import builtin_skill_root`。

若 `SkillTrustManager` 对 BUILTIN 作用域仍强制要求信任记录，则在此处显式信任内置 Skill；
先用以下命令确认其行为，再决定是否需要该调用：

Run: `grep -n "BUILTIN\|def trust\|def is_trusted" packages/aihi/agent/src/aihi/agent/skills/trust.py | head -20`

- [ ] **Step 7: 跑测试确认通过**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 全部通过

- [ ] **Step 8: 提交**

```bash
git add -A packages/aihi/code-agent
git commit -m "feat(code-agent): ship builtin coding skills under implicit trust"
```

---

### Task 5: `SubagentTool` 支持命名 Runner 映射

**Files:**
- Modify: `packages/aihi/agent/src/aihi/agent/agents/subagent.py`（`SubagentTool.__init__` 与 `run`、`spec`）
- Modify: `packages/aihi/agent/src/aihi/agent/builder.py`（`with_subagents(runners=...)`）
- Test: `packages/aihi/agent/tests/unit/test_subagent_named_runners.py`

**Interfaces:**
- Consumes: 既有 `SubagentRunner`、`SubagentAuthority`、`TaskGraph`
- Produces:
  - `SubagentTool(runner_or_runners: SubagentRunner | Mapping[str, SubagentRunner], *, authority)`
  - `task` 工具输入新增可选 `agent_type: str`，默认 `"general"`
  - `RuntimeBuilder.with_subagents(..., runners: Mapping[str, SubagentRunner] | None = None)`

- [ ] **Step 1: 写失败测试**

创建 `packages/aihi/agent/tests/unit/test_subagent_named_runners.py`。测试须证明**单实例单
TaskGraph**——这正是命名类型引入的新失效模式：

```python
from __future__ import annotations

import pytest
from aihi.agent import AgentBudget, SubagentAuthority, SubagentTool, WorkspaceScope
from aihi.agent.agents.types import TaskResult, TaskSpec


class RecordingRunner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    async def run(self, spec: TaskSpec, context: object) -> TaskResult:
        self.calls.append(spec.objective)
        return TaskResult(task_id=spec.task_id, state="completed", summary=self.label)


def _authority() -> SubagentAuthority:
    return SubagentAuthority(
        budget=AgentBudget(max_tokens=1000, timeout_seconds=30.0, max_tool_calls=5),
        workspace=WorkspaceScope(root="/tmp", read_only=True),
        max_children=2,
    )


def test_named_runners_dispatch_by_agent_type() -> None:
    explore, general = RecordingRunner("explore"), RecordingRunner("general")
    tool = SubagentTool({"explore": explore, "general": general}, authority=_authority())
    assert "agent_type" in tool.spec.input_schema["properties"]


def test_a_single_runner_stays_supported() -> None:
    tool = SubagentTool(RecordingRunner("only"), authority=_authority())
    assert tool.spec.name == "task"


def test_unknown_agent_type_is_rejected() -> None:
    tool = SubagentTool({"general": RecordingRunner("g")}, authority=_authority())
    with pytest.raises(KeyError):
        tool.runner_for("nope")


def test_runner_mapping_requires_a_general_key() -> None:
    with pytest.raises(ValueError, match="general"):
        SubagentTool({"explore": RecordingRunner("e")}, authority=_authority())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/agent/tests/unit/test_subagent_named_runners.py -v`
Expected: FAIL

- [ ] **Step 3: 改 `SubagentTool`**

`spec.input_schema["properties"]` 增加 `"agent_type": {"type": "string"}`，描述补一句说明可选类型。
`__init__` 与分派：

```python
    def __init__(
        self,
        runner: SubagentRunner | Mapping[str, SubagentRunner],
        *,
        authority: SubagentAuthority,
    ) -> None:
        if isinstance(runner, Mapping):
            if "general" not in runner:
                raise ValueError("Named subagent runners must include a 'general' entry")
            self.runners: dict[str, SubagentRunner] = dict(runner)
        else:
            self.runners = {"general": runner}
        self.authority = authority
        # One graph per (session, run) regardless of how many agent types exist:
        # per-type graphs would count max_children per type and defeat the ceiling.
        self._graphs: dict[tuple[str, str], tuple[TaskGraph, str]] = {}

    @property
    def runner(self) -> SubagentRunner:
        return self.runners["general"]

    def runner_for(self, agent_type: str) -> SubagentRunner:
        return self.runners[agent_type]
```

在 `run()` 中，解析 objective 之后加入：

```python
        agent_type = input.get("agent_type", "general")
        if not isinstance(agent_type, str) or not agent_type.strip():
            raise AgentValidationError("agent_type must be a non-empty string")
        try:
            runner = self.runner_for(agent_type.strip())
        except KeyError:
            return ToolExecutionResult(
                content=f"Unknown subagent type: {agent_type}",
                is_error=True,
                metadata={"error_code": "subagent_type_unknown"},
            )
```

并把后续 `await self.runner.run(...)` 改为 `await runner.run(...)`。
新增 import：`from collections.abc import Mapping`。

- [ ] **Step 4: 改 `builder.with_subagents`**

签名增加 `runners: Mapping[str, SubagentRunner] | None = None`，存入 `_SubagentPlan`；
`_subagent_tool` 末尾改为：

```python
        default_runner = ChildRunSubagentRunner(...)  # 保持既有构造
        return SubagentTool(plan.runners or default_runner, authority=plan.authority)
```

`_SubagentPlan` 增加 `runners: Mapping[str, SubagentRunner] | None = None` 字段。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest packages/aihi/agent/tests -q && python3 -m mypy`
Expected: 全部通过；既有 subagent 测试不得回归

- [ ] **Step 6: 提交**

```bash
git add packages/aihi/agent
git commit -m "feat(agent): let one subagent tool dispatch to named runners"
```

---

### Task 6: 命名 Subagent 类型

**Files:**
- Create: `packages/aihi/code-agent/src/aihi/code_agent/subagents/__init__.py`
- Create: `packages/aihi/code-agent/src/aihi/code_agent/subagents/registry.py`
- Create: `.../subagents/prompts/explore.md`、`code_review.md`、`test.md`、`general.md`
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/config.py`（`subagents.types`、默认开启）
- Modify: `packages/aihi/code-agent/src/aihi/code_agent/runtime.py`（构造命名 Runner）
- Test: `packages/aihi/code-agent/tests/test_subagents.py`

**Interfaces:**
- Consumes: Task 5 的 `SubagentTool(mapping)` 与 `with_subagents(runners=...)`
- Produces:
  - `SubagentDefinition(name, description, prompt_file, capabilities, tools=None, model=None)`
  - `CODING_SUBAGENTS: tuple[SubagentDefinition, ...]`
  - `build_subagent_runners(config, *, plan_provider, plan_model, store, sandbox, registry, policy) -> dict[str, SubagentRunner]`

- [ ] **Step 1: 写失败测试**

创建 `packages/aihi/code-agent/tests/test_subagents.py`：

```python
from __future__ import annotations

from aihi.code_agent.config import load_config
from aihi.code_agent.subagents import CODING_SUBAGENTS, definition_for


def test_general_is_defined_and_names_are_unique() -> None:
    names = [definition.name for definition in CODING_SUBAGENTS]
    assert "general" in names
    assert len(names) == len(set(names))


def test_every_definition_has_a_packaged_prompt() -> None:
    for definition in CODING_SUBAGENTS:
        assert definition.prompt().strip()


def test_read_only_types_never_request_write_capabilities() -> None:
    for name in ("explore", "code_review"):
        capabilities = definition_for(name).capabilities
        assert not any("write" in capability for capability in capabilities)
        assert not any("process" in capability for capability in capabilities)


def test_defaults_enable_subagents_with_a_read_only_ceiling(tmp_path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.subagents.enabled is True
    assert config.subagents.capabilities == frozenset({"filesystem.read"})
    assert config.subagents.max_depth == 1
    assert config.subagents.max_children == 3


def test_config_can_override_one_type(tmp_path) -> None:
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[subagents]\nenabled = true\n\n'
        '[subagents.types.test]\nenabled = false\n',
        encoding="utf-8",
    )
    config = load_config(path, cwd=tmp_path)
    assert config.subagents.types["test"].enabled is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest packages/aihi/code-agent/tests/test_subagents.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'aihi.code_agent.subagents'`

- [ ] **Step 3: 写 `subagents/registry.py`**

```python
"""Named Subagent types owned by the Coding Agent application."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    """One delegatable role: its prompt, tool subset and capability ceiling."""

    name: str
    description: str
    prompt_file: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    tools: tuple[str, ...] | None = None
    model: str | None = None

    def prompt(self) -> str:
        resource = files("aihi.code_agent.subagents") / "prompts" / self.prompt_file
        return resource.read_text(encoding="utf-8").strip()


__all__ = ["SubagentDefinition"]
```

- [ ] **Step 4: 写 `subagents/__init__.py` 与四个提示词**

```python
"""The Coding Agent subagent roster."""

from __future__ import annotations

from .registry import SubagentDefinition

CODING_SUBAGENTS: tuple[SubagentDefinition, ...] = (
    SubagentDefinition(
        name="explore",
        description="Read-only search across the repository. Returns findings, not edits.",
        prompt_file="explore.md",
        capabilities=frozenset({"filesystem.read"}),
        tools=("read_file", "glob", "grep"),
    ),
    SubagentDefinition(
        name="code_review",
        description="Read-only review of the current diff for correctness defects.",
        prompt_file="code_review.md",
        capabilities=frozenset({"filesystem.read"}),
        tools=("read_file", "glob", "grep", "git_status", "git_diff"),
    ),
    SubagentDefinition(
        name="test",
        description="Write and run tests. Requires process execution, off by default.",
        prompt_file="test.md",
        capabilities=frozenset({"filesystem.read", "filesystem.write", "process.spawn"}),
        tools=("read_file", "glob", "grep", "edit_file", "write_file", "bash"),
    ),
    SubagentDefinition(
        name="general",
        description="A scoped objective inheriting this run's capabilities.",
        prompt_file="general.md",
    ),
)


def definition_for(name: str) -> SubagentDefinition:
    for definition in CODING_SUBAGENTS:
        if definition.name == name:
            return definition
    raise KeyError(f"Unknown subagent type: {name}")


__all__ = ["CODING_SUBAGENTS", "SubagentDefinition", "definition_for"]
```

`prompts/explore.md` 正文：

```markdown
You are a read-only exploration subagent. You cannot modify anything.

Locate what was asked for and report it. Search broadly with `glob` and `grep`
before reading; read only the parts of a file you need. Return concrete
`path:line` references and a short statement of what each one does. Do not
propose changes, and do not summarise code you did not actually open.
```

`code_review.md`、`test.md`、`general.md` 同样写明角色、可用工具边界与报告格式。

- [ ] **Step 5: 扩展 `config.py`**

`SubagentSettings` 增加：

```python
    enabled: bool = True
    max_depth: int = 1
    max_children: int = 3
    types: Mapping[str, SubagentTypeSettings] = field(default_factory=dict)
```

新增 `SubagentTypeSettings(enabled: bool = True, model: str | None = None)`，
并在 `_parse_subagents` 中解析 `[subagents.types.<name>]` 子表；未知类型名报
`CodeAgentConfigError`。`capabilities` 默认值保持 `frozenset({"filesystem.read"})`。

- [ ] **Step 6: 在 `runtime.py` 构造命名 Runner**

在 `config.subagents.enabled` 分支内，为每个启用的定义构造一个带专属提示词与模型的
`ChildRunSubagentRunner`，键为定义名，传给 `builder.with_subagents(..., runners=runners)`。
`SubagentAuthority` 的 `max_depth` / `max_children` 取自配置。

- [ ] **Step 7: 跑测试确认通过**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 全部通过

既有测试 `test_runtime_composes_configured_artifacts_compaction_and_subagents` 断言
`registry.get("task") is not None`，应继续通过。

- [ ] **Step 8: 提交**

```bash
git add -A packages/aihi/code-agent
git commit -m "feat(code-agent): add named coding subagent types"
```

---

### Task 7: 打包资产与 wheel 断言

**Files:**
- Modify: `packages/aihi/code-agent/pyproject.toml`
- Test: `tests/packaging/test_code_agent_assets.py`

**Interfaces:**
- Consumes: Task 3、4、6 产出的 `.md` 资产
- Produces: 无新代码接口

- [ ] **Step 1: 写失败测试**

创建 `tests/packaging/test_code_agent_assets.py`：

```python
"""The packaged Coding Agent must carry its prompts and skills."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY / "packages" / "aihi" / "code-agent"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("code-agent-wheel")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(output), str(PACKAGE)],
        cwd=REPOSITORY, check=True, capture_output=True, text=True,
    )
    return next(output.glob("aihi_code_agent-*.whl"))


def test_wheel_carries_prompts_and_builtin_skills(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "aihi/code_agent/prompts/coding.md" in names
    assert "aihi/code_agent/skills/builtin/code_review.md" in names
    assert any(
        name.startswith("aihi/code_agent/subagents/prompts/") and name.endswith(".md")
        for name in names
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/packaging/test_code_agent_assets.py -v`
Expected: FAIL —— 资产缺失，正是本任务要证明的问题

- [ ] **Step 3: 登记资产**

`packages/aihi/code-agent/pyproject.toml`：

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/aihi"]
artifacts = [
  "src/aihi/code_agent/py.typed",
  "src/aihi/code_agent/prompts/*.md",
  "src/aihi/code_agent/skills/builtin/*.md",
  "src/aihi/code_agent/subagents/prompts/*.md",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/packaging/test_code_agent_assets.py -v`
Expected: PASS

- [ ] **Step 5: 全量验证**

Run: `python3 -m pytest && python3 -m ruff check . && python3 -m mypy`
Expected: 全部通过

- [ ] **Step 6: 更新 README 并提交**

在 `packages/aihi/code-agent/README.md` 补一节，说明三处行为变更：`system_prompt` 默认追加、
BUILTIN Skill 免锁文件、`subagents.enabled` 默认开启但授权为只读。

```bash
git add -A packages/aihi/code-agent tests/packaging
git commit -m "build(code-agent): ship prompts and builtin skills in the wheel"
```

---

## Self-Review

**Spec coverage：** RFC §设计.1 → Task 1；§设计.2 → Task 2；§设计.3 → Task 3；§设计.4 →
Task 4；§设计.5 的 `aihi.agent` 扩展 → Task 5，类型注册表与配置 → Task 6；§打包 → Task 7。
RFC「不变式」中的「单 Run 单 TaskGraph」由 Task 5 Step 1 的测试覆盖；「授权只能收窄」由
Task 6 Step 1 的只读能力断言覆盖。

**未覆盖项（有意）：** RFC §分期 明确将协议流式化与 `client.ts:151` 的 30s 超时列为下一轮，
本计划不含。`resume()` 改为消费 `stream()` 亦推迟至协议轮，因当前 Worker 仍依赖其同步语义。

**已知需现场确认的两处**（已写成命令而非假设）：Task 1 Step 6 的 `tool.*` 事件名、
Task 4 Step 1 的 Skill frontmatter 字段与 Step 6 的 `SkillTrustManager` 对 BUILTIN 的行为。
