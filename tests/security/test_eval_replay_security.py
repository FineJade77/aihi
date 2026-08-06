from aiharness.core.events import Event
from aiharness.evals import ReplayEngine, TraceBundle


def test_trace_export_redacts_credentials_before_replay_storage() -> None:
    trace = TraceBundle.from_events(
        [
            Event(
                type="provider.request",
                session_id="ses-secret",
                seq=1,
                data={"authorization": "Bearer secret-token-value", "safe": "ok"},
            )
        ]
    )
    serialized = str(trace.to_dict())
    assert "secret-token-value" not in serialized
    assert "safe" in serialized
    assert ReplayEngine().replay(trace).event_count == 1
