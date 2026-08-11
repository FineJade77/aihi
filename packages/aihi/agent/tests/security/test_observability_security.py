from aihi.agent._core.events import Event
from aihi.agent.observability import InMemoryTelemetrySink, Telemetry


def test_event_observation_never_retains_common_credentials() -> None:
    sink = InMemoryTelemetrySink()
    Telemetry(sink).record_event(
        Event(
            type="provider.request",
            session_id="ses-security",
            data={
                "authorization": "Bearer super-secret-token",
                "nested": {"client_secret": "secret-value", "safe": "ok"},
            },
        )
    )
    serialized = str(sink.records()[0].to_dict())
    assert "super-secret-token" not in serialized
    assert "secret-value" not in serialized
    assert "safe" in serialized
