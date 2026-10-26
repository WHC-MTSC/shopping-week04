from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from workload.log_delivery import LocalS3Sink, LogDelivery, LogSettings, S3Sink, build_sink
from workload.log_models import (
    CountFeedback,
    Event,
    FeedbackEvent,
    FeedbackRequest,
    PredictionEvent,
)
from workload.log_store import LogStore, PredictionMissingError
from workload.schemas import ForecastRequest, ForecastResponse


def example_event() -> PredictionEvent:
    return PredictionEvent(
        event_id=uuid4(),
        prediction_id=uuid4(),
        recorded_at=datetime.now(UTC),
        data_source="demo",
        request=ForecastRequest.model_validate_json(
            Path("examples/request.json").read_text(encoding="utf-8")
        ),
        response=ForecastResponse.model_validate_json(
            Path("examples/response.json").read_text(encoding="utf-8")
        ),
        features={},
        model_sha256="test-model-hash",
    )


class FlakySink:
    def __init__(self) -> None:
        self.offline: bool = True
        self.events: list[Event] = []

    def send(self, event: Event) -> None:
        if self.offline:
            raise EndpointConnectionError(endpoint_url="https://s3.invalid")
        self.events.append(event)


def test_failed_upload_remains_durable_and_retries_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = LogStore(path, remote=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    sink = FlakySink()
    assert LogDelivery(store, sink).flush() == 0
    assert store.status("ap-northeast-2").failed_events == 1
    assert store.status("ap-northeast-2").last_error == "EndpointConnectionError"
    assert store.get_prediction(event.prediction_id).log_status == "pending"
    restarted = LogStore(path, remote=True)
    sink.offline = False
    assert LogDelivery(restarted, sink).flush() == 1
    assert restarted.get_prediction(event.prediction_id).log_status == "sent"
    assert restarted.status("ap-northeast-2").pending_events == 0
    assert restarted.status("ap-northeast-2").last_error is None
    assert sink.events == [event]
    assert LogDelivery(restarted, sink).flush() == 0


def test_local_mode_does_not_send_or_acknowledge(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db")
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    assert LogDelivery(store, None).flush() == 0
    assert store.get_prediction(event.prediction_id).log_status == "local"
    assert len(store.pending()) == 1


def test_s3_request_is_encrypted_immutable_and_duplicate_safe() -> None:
    session = boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - Stubber never sends these test credentials.
        region_name="ap-northeast-2",
    )
    client = session.client("s3")  # pyright: ignore[reportUnknownMemberType]
    settings = LogSettings(bucket="test-workload-bucket")
    event = example_event()
    params = {
        "Bucket": "test-workload-bucket",
        "Key": f"workload-events/prediction/{event.recorded_at:%Y/%m/%d}/{event.event_id}.json",
        "Body": event.model_dump_json().encode("utf-8"),
        "ContentType": "application/json",
        "ServerSideEncryption": "AES256",
        "IfNoneMatch": "*",
    }
    with Stubber(client) as stubber:
        stubber.add_response("put_object", {}, params)
        stubber.add_client_error(
            "put_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params=params,
        )
        body = event.model_dump_json().encode("utf-8")
        raw = BytesIO(body)
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(raw, len(body)), "ContentLength": len(body)},
            {"Bucket": params["Bucket"], "Key": params["Key"]},
        )
        sink = S3Sink(settings, client)
        sink.send(event)
        sink.send(event)
        assert raw.closed
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("existing_content", ["different", "shorter", "longer", "denied"])
def test_s3_key_collision_requires_matching_readable_body(
    tmp_path: Path, existing_content: str
) -> None:
    session = boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - Stubber never sends these test credentials.
        region_name="ap-northeast-2",
    )
    client = session.client("s3")  # pyright: ignore[reportUnknownMemberType]
    settings = LogSettings(bucket="test-workload-bucket")
    store = LogStore(tmp_path / "events.db", remote=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    body = event.model_dump_json().encode("utf-8")
    raw = BytesIO(
        b"x" * len(body)
        if existing_content == "different"
        else body[:-1]
        if existing_content == "shorter"
        else body + b"x"
    )
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        if existing_content == "denied":
            stubber.add_client_error(
                "get_object", service_error_code="AccessDenied", http_status_code=403
            )
        else:
            length = raw.getbuffer().nbytes
            stubber.add_response(
                "get_object", {"Body": StreamingBody(raw, length), "ContentLength": length}
            )
        assert LogDelivery(store, S3Sink(settings, client)).flush() == 0
        stubber.assert_no_pending_responses()
    assert store.get_prediction(event.prediction_id).log_status == "pending"
    assert store.status(settings.region).failed_events == 1
    assert store.status(settings.region).last_error in (
        {"ClientError", "AccessDenied"} if existing_content == "denied" else {"ValueError"}
    )
    if existing_content != "denied":
        assert raw.closed


def test_permission_failure_stays_pending(tmp_path: Path) -> None:
    session = boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - Stubbed request only.
        region_name="ap-northeast-2",
    )
    client = session.client("s3")  # pyright: ignore[reportUnknownMemberType]
    store = LogStore(tmp_path / "events.db", remote=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="AccessDenied", http_status_code=403
        )
        assert LogDelivery(store, S3Sink(LogSettings(bucket="test-bucket"), client)).flush() == 0
    assert store.get_prediction(event.prediction_id).log_status == "pending"


def feedback_event(prediction: PredictionEvent) -> FeedbackEvent:
    submission = FeedbackRequest(
        submission_id=uuid4(), feedback=CountFeedback(kind="actual_count", value=0)
    )
    return FeedbackEvent(
        event_id=submission.submission_id,
        prediction_id=prediction.prediction_id,
        recorded_at=datetime.now(UTC),
        submission=submission,
        actual_stage="idle",
        is_correct=True,
    )


def test_concurrent_prediction_retries_create_one_event(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db")
    store.initialize()
    event = example_event()
    requests = [event.model_copy(update={"event_id": uuid4()}) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(store.save_prediction, requests))
    assert len(store.pending()) == 1
    assert all(result.event == results[0].event for result in results)


def test_concurrent_feedback_retries_create_one_revision(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db")
    store.initialize()
    prediction = example_event()
    _ = store.save_prediction(prediction)
    feedback = feedback_event(prediction)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(store.save_feedback, [feedback] * 8))
    assert len(store.pending()) == 2
    assert all(result.event == results[0].event for result in results)
    assert results[0].event.revision == 1


def test_concurrent_corrections_have_unambiguous_s3_revision_order(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db")
    store.initialize()
    prediction = example_event()
    _ = store.save_prediction(prediction)
    corrections = [feedback_event(prediction) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(store.save_feedback, corrections))
    assert sorted(receipt.event.revision for receipt in receipts) == list(range(1, 9))
    assert len(store.pending()) == 9
    detail = store.get_prediction(prediction.prediction_id)
    assert detail.latest_feedback is not None
    assert detail.latest_feedback.event.revision == 8


def test_store_rejects_feedback_without_its_prediction(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db")
    store.initialize()
    with pytest.raises(PredictionMissingError):
        _ = store.save_feedback(feedback_event(example_event()))
    assert store.pending() == []


def test_local_s3_stores_real_json_and_retries_identically(tmp_path: Path) -> None:
    settings = LogSettings(backend="local-s3", local_s3_root=tmp_path / "objects")
    sink = LocalS3Sink(settings)
    event = example_event()
    sink.send(event)
    path = sink.object_path(event)
    expected = (
        tmp_path
        / "objects/hourahead-local-logs/workload-events/prediction"
        / f"{event.recorded_at:%Y/%m/%d}/{event.event_id}.json"
    )
    assert path == expected
    assert PredictionEvent.model_validate_json(path.read_bytes()) == event
    with ThreadPoolExecutor(max_workers=4) as executor:
        _ = list(executor.map(sink.send, [event] * 4))
    assert list((tmp_path / "objects").rglob("*.json")) == [path]
    assert not list((tmp_path / "objects").rglob("*.tmp"))
    changed = event.model_copy(update={"model_sha256": "different"})
    with pytest.raises(ValueError, match="different event"):
        sink.send(changed)
    assert PredictionEvent.model_validate_json(path.read_bytes()) == event


@pytest.mark.parametrize(
    "prefix", ["../escape", "safe/../../escape", r"C:\outside", "CON", "x/.. "]
)
def test_local_s3_rejects_unsafe_paths(tmp_path: Path, prefix: str) -> None:
    sink = LocalS3Sink(
        LogSettings(backend="local-s3", local_s3_root=tmp_path / "objects", prefix=prefix)
    )
    with pytest.raises(ValueError, match="Unsafe"):
        sink.send(example_event())
    assert not (tmp_path / "objects").exists()


def test_failed_local_s3_write_is_not_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LogStore(tmp_path / "events.db", simulated=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    sink = LocalS3Sink(LogSettings(backend="local-s3", local_s3_root=tmp_path / "objects"))

    def fail_link(_source: Path, _target: Path) -> None:
        raise PermissionError

    monkeypatch.setattr("workload.log_delivery.os.link", fail_link)
    assert LogDelivery(store, sink).flush() == 0
    assert store.get_prediction(event.prediction_id).log_status == "pending"
    assert store.status("ap-northeast-2").failed_events == 1
    assert not sink.object_path(event).exists()
    assert not list((tmp_path / "objects").rglob("*.tmp"))


def test_simulated_delivery_does_not_count_as_real_aws_delivery(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    simulated = LogStore(database, simulated=True)
    simulated.initialize()
    event = example_event()
    _ = simulated.save_prediction(event)
    sink = LocalS3Sink(LogSettings(backend="local-s3", local_s3_root=tmp_path / "objects"))
    assert LogDelivery(simulated, sink).flush() == 1
    assert simulated.get_prediction(event.prediction_id).log_status == "simulated"
    assert simulated.status("ap-northeast-2").destination == "local-s3"
    assert simulated.status("ap-northeast-2").sent_events == 1
    real = LogStore(database, remote=True)
    assert real.get_prediction(event.prediction_id).log_status == "pending"
    assert real.status("ap-northeast-2").sent_events == 0
    assert real.pending() == [event]
    simulated.acknowledge(event.event_id)
    assert real.get_prediction(event.prediction_id).log_status == "pending"
    real.acknowledge(event.event_id)
    assert real.get_prediction(event.prediction_id).log_status == "sent"
    assert simulated.get_prediction(event.prediction_id).log_status == "simulated"


def test_local_s3_factory_never_initializes_aws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_session() -> None:
        pytest.fail("Local S3 must not construct an AWS session")

    monkeypatch.setattr("workload.log_delivery.boto3.Session", reject_session)
    settings = LogSettings(
        backend="local-s3", bucket="unused-real-bucket", local_s3_root=tmp_path / "objects"
    )
    sink = build_sink(settings)
    assert isinstance(sink, LocalS3Sink)
    sink.send(example_event())


def test_backend_configuration_defaults_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("WORKLOAD_LOG_BACKEND", "WORKLOAD_S3_BUCKET", "WORKLOAD_LOCAL_S3_ROOT"):
        monkeypatch.delenv(name, raising=False)
    assert LogSettings.from_env().backend == "local"
    assert LogSettings(bucket="configured-bucket").backend == "s3"
    monkeypatch.setenv("WORKLOAD_S3_BUCKET", "configured-bucket")
    assert LogSettings.from_env().backend == "s3"
    monkeypatch.setenv("WORKLOAD_LOG_BACKEND", "local-s3")
    monkeypatch.setenv("WORKLOAD_LOCAL_S3_ROOT", str(tmp_path / "objects"))
    configured = LogSettings.from_env()
    assert configured.backend == "local-s3"
    assert configured.local_s3_root == tmp_path / "objects"
    monkeypatch.setenv("WORKLOAD_LOG_BACKEND", "unknown")
    with pytest.raises(ValueError, match="local-s3"):
        _ = LogSettings.from_env()
