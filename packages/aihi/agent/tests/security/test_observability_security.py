import json

from aihi.agent._core.events import Event
from aihi.agent.observability import InMemoryTelemetrySink, JsonlTelemetrySink, Telemetry


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


def test_jsonl_audit_file_is_private_and_redacted(tmp_path) -> None:
    path = tmp_path / ".aihi" / "audit.jsonl"
    telemetry = Telemetry(JsonlTelemetrySink(path))
    telemetry.record_event(
        Event(
            type="provider.request",
            session_id="ses-security-file",
            data={"authorization": "Bearer file-secret-token", "safe": "ok"},
        )
    )
    telemetry.close()

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["data"]["authorization"] == "[REDACTED]"
    assert "file-secret-token" not in raw
    assert path.stat().st_mode & 0o777 == 0o600
