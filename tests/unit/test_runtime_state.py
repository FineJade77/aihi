import pytest

from aiharness.runtime import InvalidRunTransition, RunState, RunStateMachine


def test_run_state_machine_accepts_recovery_path_and_rejects_terminal_reuse() -> None:
    machine = RunStateMachine()

    assert machine.transition(RunState.RUNNING) == RunState.RUNNING
    assert machine.transition(RunState.WAITING_TOOL) == RunState.WAITING_TOOL
    assert machine.transition(RunState.RUNNING) == RunState.RUNNING
    assert machine.transition(RunState.COMPLETED) == RunState.COMPLETED
    with pytest.raises(InvalidRunTransition):
        machine.transition(RunState.RUNNING)
