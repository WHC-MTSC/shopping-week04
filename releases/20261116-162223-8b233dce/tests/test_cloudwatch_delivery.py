from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from tests.test_log_delivery import example_event
from workload.cloudwatch_delivery import (
    MAX_BATCH_BYTES,
    CloudWatchPayloadTooLargeError,
    CloudWatchSink,
)
from workload.log_delivery import LogDelivery, LogSettings, build_sink
from workload.log_models import PredictionEvent
from workload.log_store import LogStore


class FakeLogsClient:
    def __init__(self, responses: list[dict[str, object] | Exception] | None = None) -> None:
        self.responses: deque[dict[str, object] | Exception] = deque(responses or [{}])
        self.calls: list[dict[str, object]] = []

    def put_log_events(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def settings() -> LogSettings:
    return LogSettings(
        backend="cloudwatch",
        log_group="/hourahead/existing",
        log_stream="existing-stream",
    )


def test_upload_preserves_original_event_and_uses_current_timestamp(
    settings: LogSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch_ns = 1_800_000_000_123_456_789
    monkeypatch.setattr("workload.cloudwatch_delivery.time.time_ns", lambda: epoch_ns)
    event = example_event().model_copy(update={"recorded_at": datetime(2000, 1, 1, tzinfo=UTC)})
    client = FakeLogsClient()
    CloudWatchSink(settings, client).send(event)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call) == {"logGroupName", "logStreamName", "logEvents"}
    assert call["logGroupName"] == settings.log_group
    assert call["logStreamName"] == settings.log_stream
    records = cast("list[dict[str, object]]", call["logEvents"])
    assert len(records) == 1
    assert records[0]["timestamp"] == epoch_ns // 1_000_000
    message = cast("str", records[0]["message"])
    assert PredictionEvent.model_validate_json(message) == event


@pytest.mark.parametrize(
    "rejection",
    [
        {},
        {"tooOldLogEventEndIndex": 0},
        {"tooNewLogEventStartIndex": 0},
        {"expiredLogEventEndIndex": 0},
    ],
)
def test_http_success_with_rejection_remains_pending_and_retries(
    settings: LogSettings,
    tmp_path: Path,
    rejection: dict[str, int],
) -> None:
    store = LogStore(tmp_path / "logs.db", cloudwatch=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    client = FakeLogsClient([{"rejectedLogEventsInfo": rejection}, {}])
    delivery = LogDelivery(store, CloudWatchSink(settings, client))
    assert delivery.flush() == 0
    assert store.get_prediction(event.prediction_id).log_status == "pending"
    status = store.status(settings.region)
    assert status.destination == "cloudwatch"
    assert status.failed_events == 1
    assert status.last_error == "CloudWatchRejectedEventsError"
    assert delivery.flush() == 1
    assert store.get_prediction(event.prediction_id).log_status == "cloudwatch_sent"
    assert store.pending() == []
    assert store.status(settings.region).last_error is None


def test_ambiguous_network_failure_retries_same_event_id_at_least_once(
    settings: LogSettings,
    tmp_path: Path,
) -> None:
    store = LogStore(tmp_path / "logs.db", cloudwatch=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    client = FakeLogsClient([EndpointConnectionError(endpoint_url="https://logs.invalid"), {}])
    assert LogDelivery(store, CloudWatchSink(settings, client)).flush() == 0
    restarted = LogStore(store.path, cloudwatch=True)
    assert restarted.pending() == [event]
    assert LogDelivery(restarted, CloudWatchSink(settings, client)).flush() == 1
    messages = [
        cast("list[dict[str, object]]", call["logEvents"])[0]["message"] for call in client.calls
    ]
    assert messages == [event.model_dump_json(), event.model_dump_json()]


@pytest.mark.parametrize("code", ["ResourceNotFoundException", "AccessDeniedException"])
def test_existing_resource_or_permission_failure_never_acknowledges(
    settings: LogSettings,
    tmp_path: Path,
    code: str,
) -> None:
    store = LogStore(tmp_path / "logs.db", cloudwatch=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    client = FakeLogsClient(
        [
            ClientError(
                {"Error": {"Code": code, "Message": "SECRET"}},
                "PutLogEvents",
            )
        ]
    )
    assert LogDelivery(store, CloudWatchSink(settings, client)).flush() == 0
    assert store.pending() == [event]
    assert store.status(settings.region).last_error == "ClientError"
    assert len(client.calls) == 1


def test_s3_simulation_and_cloudwatch_acknowledgments_are_independent(tmp_path: Path) -> None:
    path = tmp_path / "logs.db"
    s3 = LogStore(path, remote=True)
    simulated = LogStore(path, simulated=True)
    cloudwatch = LogStore(path, cloudwatch=True)
    s3.initialize()
    event = example_event()
    _ = s3.save_prediction(event)
    s3.acknowledge(event.event_id)
    simulated.acknowledge(event.event_id)
    assert cloudwatch.pending() == [event]
    assert cloudwatch.get_prediction(event.prediction_id).log_status == "pending"
    assert cloudwatch.status("ap-northeast-2").sent_events == 0
    cloudwatch.acknowledge(event.event_id)
    assert cloudwatch.pending() == []
    assert cloudwatch.get_prediction(event.prediction_id).log_status == "cloudwatch_sent"
    assert s3.get_prediction(event.prediction_id).log_status == "sent"
    assert simulated.get_prediction(event.prediction_id).log_status == "simulated"
    with cloudwatch.connection() as connection:
        assert connection.execute("SELECT sent FROM events").fetchone() == (7,)


def test_cloudwatch_ack_does_not_acknowledge_s3_or_simulation(tmp_path: Path) -> None:
    path = tmp_path / "logs.db"
    cloudwatch = LogStore(path, cloudwatch=True)
    cloudwatch.initialize()
    event = example_event()
    _ = cloudwatch.save_prediction(event)
    cloudwatch.acknowledge(event.event_id)
    assert LogStore(path, remote=True).pending() == [event]
    assert LogStore(path, simulated=True).pending() == [event]
    assert LogStore(path).get_prediction(event.prediction_id).log_status == "cloudwatch_sent"


def test_cloudwatch_factory_is_lazy_and_never_creates_resources(
    settings: LogSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_session() -> None:
        pytest.fail("The factory must not resolve AWS credentials or create resources")

    monkeypatch.setattr("workload.cloudwatch_delivery.boto3.Session", reject_session)
    assert isinstance(build_sink(settings), CloudWatchSink)


def test_oversized_payload_is_rejected_before_sdk_call(settings: LogSettings) -> None:
    event = example_event().model_copy(update={"features": {"x" * MAX_BATCH_BYTES: 0.0}})
    client = FakeLogsClient()
    with pytest.raises(CloudWatchPayloadTooLargeError):
        CloudWatchSink(settings, client).send(event)
    assert client.calls == []


def test_cloudwatch_requires_explicit_existing_group_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKLOAD_LOG_BACKEND", "cloudwatch")
    for name in ("WORKLOAD_CLOUDWATCH_LOG_GROUP", "WORKLOAD_CLOUDWATCH_LOG_STREAM"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="WORKLOAD_CLOUDWATCH_LOG_GROUP"):
        _ = LogSettings.from_env()
    monkeypatch.setenv("WORKLOAD_CLOUDWATCH_LOG_GROUP", "/hourahead/existing")
    with pytest.raises(ValueError, match="WORKLOAD_CLOUDWATCH_LOG_STREAM"):
        _ = LogSettings.from_env()
    monkeypatch.setenv("WORKLOAD_CLOUDWATCH_LOG_STREAM", "existing-stream")
    configured = LogSettings.from_env()
    assert configured.backend == "cloudwatch"
    assert configured.log_group == "/hourahead/existing"
    assert configured.log_stream == "existing-stream"


@pytest.mark.parametrize(
    "options",
    [
        {"cloudwatch": True, "remote": True},
        {"cloudwatch": True, "simulated": True},
    ],
)
def test_store_rejects_multiple_destinations(tmp_path: Path, options: dict[str, bool]) -> None:
    with pytest.raises(ValueError, match="multiple delivery destinations"):
        _ = LogStore(tmp_path / "logs.db", **options)
