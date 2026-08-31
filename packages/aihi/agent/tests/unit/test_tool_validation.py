"""Regression tests for validation and side-effect-free tool preparation."""

from pathlib import Path
from typing import Any

import pytest
from aihi.agent import (
    Decision,
    DecisionEffect,
    HostBackend,
    PermissionContext,
    PermissionMode,
    PreparedToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolInputError,
    ToolRegistry,
    ToolSpec,
)
from aihi.agent.tools import ToolDispatcher
from aihi.agent.tools.base import validate_tool_input
from aihi.models import ToolCallBlock


@pytest.mark.parametrize("schema_type", ["integer", "number"])
def test_json_schema_numbers_reject_booleans(schema_type: str) -> None:
    spec = ToolSpec.define(
        name="numeric_tool",
        description="Accept a number",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": schema_type}},
            "required": ["value"],
        },
        concurrency_safe=True,
        mutates=False,
    )

    with pytest.raises(ToolInputError, match="wrong type"):
        validate_tool_input(spec, {"value": True})


def test_json_schema_boolean_still_accepts_booleans() -> None:
    spec = ToolSpec.define(
        name="boolean_tool",
        description="Accept a boolean",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "boolean"}},
            "required": ["value"],
        },
        concurrency_safe=True,
        mutates=False,
    )

    validate_tool_input(spec, {"value": True})


@pytest.mark.asyncio
async def test_dispatch_prepares_valid_input_before_policy_and_execution(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    class PreparingTool:
        spec = ToolSpec.define(
            name="prepared",
            description="Normalize one path",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            concurrency_safe=True,
            mutates=False,
        )

        def prepare(
            self, input: dict[str, Any], context: ToolContext[dict[str, str]]
        ) -> PreparedToolCall:
            observed["prepare_context"] = context.app_context
            return PreparedToolCall(
                input={"path": str((tmp_path / str(input["path"])).resolve())},
                execution={"transport": "local"},
            )

        async def run(
            self, input: dict[str, Any], context: ToolContext[dict[str, str]]
        ) -> ToolExecutionResult:
            observed["run_input"] = dict(input)
            observed["run_context"] = context.app_context
            return ToolExecutionResult(content=str(input["path"]))

    class RecordingPolicy:
        def evaluate(
            self,
            spec: ToolSpec,
            input: dict[str, Any],
            context: PermissionContext[dict[str, str]],
        ) -> Decision:
            observed["policy_input"] = dict(input)
            observed["policy_context"] = context.app_context
            return Decision(DecisionEffect.ALLOW, "allowed", "test.allowed")

    sandbox = HostBackend(tmp_path, unsafe=True)
    app_context = {"workspace": str(tmp_path)}
    dispatcher = ToolDispatcher(ToolRegistry([PreparingTool()]), RecordingPolicy())
    result = await dispatcher.dispatch(
        ToolCallBlock("toolu-prepared", "prepared", {"path": "note.txt"}),
        context=ToolContext(
            cwd=str(tmp_path),
            session_id="ses-prepared",
            run_id="run-prepared",
            app_context=app_context,
        ),
        permission=PermissionContext(
            cwd=tmp_path,
            mode=PermissionMode.DEFAULT,
            sandbox=sandbox.descriptor,
            run_id="run-prepared",
            app_context=app_context,
        ),
    )

    normalized = {"path": str((tmp_path / "note.txt").resolve())}
    assert result.result.is_error is False
    assert result.prepared_input == normalized
    assert result.execution == {"transport": "local"}
    assert observed == {
        "prepare_context": app_context,
        "policy_input": normalized,
        "policy_context": app_context,
        "run_input": normalized,
        "run_context": app_context,
    }
