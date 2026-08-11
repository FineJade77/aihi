"""Regression tests for the canonical tool input validator."""

import pytest
from aihi.agent import ToolInputError, ToolSpec
from aihi.agent.tools.base import validate_tool_input


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
