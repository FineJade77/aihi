"""The language-neutral Code Protocol schemas stay aligned at one wire version."""

from __future__ import annotations

import json
from pathlib import Path

from aihi.code_agent import PROTOCOL_VERSION

SCHEMAS = Path(__file__).resolve().parents[2] / "packages" / "aihi" / "code-protocol" / "schema"
PROTOCOL_PACKAGE = SCHEMAS.parent


def test_code_protocol_schemas_are_valid_json_and_versioned_03() -> None:
    assert PROTOCOL_VERSION == "0.3"
    package = json.loads((PROTOCOL_PACKAGE / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.3.0"
    paths = sorted(SCHEMAS.glob("*.schema.json"))
    assert {path.name for path in paths} == {
        "approval-descriptor.schema.json",
        "config-descriptor.schema.json",
        "event-notification.schema.json",
        "rpc-envelope.schema.json",
        "run-accepted.schema.json",
        "run-descriptor.schema.json",
        "run-error-notification.schema.json",
        "session-descriptor.schema.json",
        "task-descriptor.schema.json",
    }
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith("-0.3.json")


def test_config_schema_owns_modes_and_has_only_a_command_sandbox() -> None:
    schema = json.loads(
        (SCHEMAS / "config-descriptor.schema.json").read_text(encoding="utf-8")
    )

    assert {"access_mode", "run_mode", "command_sandbox"}.issubset(schema["required"])
    assert "permission_mode" not in schema["properties"]
    assert "workspace" not in schema["properties"]
    assert "sandbox" not in schema["properties"]
    assert "root" not in schema["properties"]["command_sandbox"]["properties"]


def test_task_schema_has_no_workspace_authority() -> None:
    schema = json.loads(
        (SCHEMAS / "task-descriptor.schema.json").read_text(encoding="utf-8")
    )
    spec = schema["properties"]["spec"]

    assert "workspace" not in spec["properties"]
    assert spec["additionalProperties"] is False


def test_run_error_schema_requires_session_and_run_identity() -> None:
    schema = json.loads(
        (SCHEMAS / "run-error-notification.schema.json").read_text(encoding="utf-8")
    )
    required = schema["properties"]["params"]["required"]
    assert required == ["protocol_version", "session_id", "run_id", "message"]
