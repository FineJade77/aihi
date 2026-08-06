from __future__ import annotations

import asyncio

import pytest
from aicode.app import build_runtime
from aicode.cli import _resume_config
from aicode.config import AICodeConfig
from typer import BadParameter

from aiharness.core.errors import UnsafeHostNotAcknowledged
from aiharness.core.types import Message
from aiharness.sessions import InMemoryEventStore, Session


def test_aicode_runtime_reuses_harness_and_requires_explicit_host_ack(tmp_path) -> None:
    with pytest.raises(UnsafeHostNotAcknowledged):
        build_runtime(AICodeConfig(workspace=tmp_path))

    runtime = build_runtime(AICodeConfig(workspace=tmp_path, unsafe_host=True))
    assert runtime.provider.name == "fake"
    assert {spec.name for spec in runtime.registry.specs} == {
        "edit_file",
        "read_file",
        "run_tests",
        "shell",
        "write_file",
    }
    assert runtime.sandbox.descriptor.unsafe is True


def test_aicode_runtime_completes_fake_session_with_harness_events(tmp_path) -> None:
    store = InMemoryEventStore()
    session = Session.create(store, cwd=tmp_path, provider="fake", model="fake-model")
    runtime = build_runtime(AICodeConfig(workspace=tmp_path, unsafe_host=True))

    result = asyncio.run(
        runtime.coordinator.run(
            session,
            model="fake-model",
            user_message=Message.text("user", "inspect this workspace"),
        )
    )

    assert result.error is None
    assert result.state.value == "completed"
    assert any(event.type == "run.completed" for event in session.events)


def test_resume_restores_persisted_workspace_provider_and_model(tmp_path) -> None:
    store = InMemoryEventStore()
    persisted_workspace = tmp_path / "persisted"
    persisted_workspace.mkdir()
    session = Session.create(
        store,
        cwd=persisted_workspace,
        provider="openai",
        model="gpt-test",
    )
    config = AICodeConfig(workspace=tmp_path, provider="fake", model="fake-model")

    resumed = _resume_config(
        config,
        session,
        workspace_explicit=False,
        provider_explicit=False,
        model_explicit=False,
    )

    assert resumed.workspace == persisted_workspace.resolve()
    assert resumed.provider == "openai"
    assert resumed.model == "gpt-test"


def test_resume_rejects_explicit_workspace_mismatch(tmp_path) -> None:
    store = InMemoryEventStore()
    persisted_workspace = tmp_path / "persisted"
    persisted_workspace.mkdir()
    session = Session.create(
        store,
        cwd=persisted_workspace,
        provider="fake",
        model="fake-model",
    )
    config = AICodeConfig(workspace=tmp_path)

    with pytest.raises(BadParameter, match="must match the persisted session workspace"):
        _resume_config(
            config,
            session,
            workspace_explicit=True,
            provider_explicit=False,
            model_explicit=False,
        )
