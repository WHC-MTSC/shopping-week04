import json
import time
from datetime import timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import typer
from botocore.exceptions import ClientError

from tests.test_log_delivery import example_event
from workload import live_check
from workload.live_check import CheckReport, run_check, safe_error
from workload.log_delivery import LogSettings, object_key
from workload.log_models import (
    Event,
    FeedbackEvent,
    FeedbackReceipt,
    FeedbackRequest,
    LogBackend,
    LoggingStatus,
    PredictionDetail,
    PredictionEvent,
    RecordedForecast,
)
from workload.schemas import ForecastRequest


class FakeAPI:
    def __init__(self, destination: LogBackend = "s3") -> None:
        self.destination: LogBackend = destination
        self.events: list[Event] = []
        self.prediction: PredictionEvent | None = None
        self.feedback: FeedbackReceipt | None = None
        self.delivered: bool = True

    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        assert 0 < timeout <= 5
        if path == "/logging":
            return (
                LoggingStatus(
                    destination=self.destination,
                    region="ap-northeast-2",
                    pending_events=0,
                    sent_events=0,
                    failed_events=0,
                    last_error=None if self.delivered else "NoCredentialsError",
                )
                .model_dump_json()
                .encode()
            )
        if path == "/predict":
            assert body is not None
            assert headers is not None
            assert headers["X-Data-Source"] == "demo"
            event = example_event().model_copy(
                update={
                    "prediction_id": UUID(headers["Idempotency-Key"]),
                    "request": ForecastRequest.model_validate_json(body),
                }
            )
            self.prediction = event
            self.events.append(event)
            return (
                RecordedForecast(
                    target_hour=event.response.target_hour,
                    workload_stage=event.response.workload_stage,
                    stage_range=event.response.stage_range,
                    probabilities=event.response.probabilities,
                    model_version=event.response.model_version,
                    prediction_id=event.prediction_id,
                    recorded_at=event.recorded_at,
                    log_status="pending",
                )
                .model_dump_json()
                .encode()
            )
        event = self.prediction
        assert event is not None
        if path.endswith("/feedback"):
            assert body is not None
            submission = FeedbackRequest.model_validate_json(body)
            assert submission.feedback.kind == "actual_count"
            assert submission.feedback.value == 0
            feedback = FeedbackEvent(
                event_id=submission.submission_id,
                prediction_id=event.prediction_id,
                recorded_at=event.recorded_at,
                data_source="demo",
                submission=submission,
                actual_stage="idle",
                is_correct=True,
            )
            self.events.append(feedback)
            self.feedback = FeedbackReceipt(event=feedback, log_status="sent")
            return self.feedback.model_dump_json().encode()
        return (
            PredictionDetail(
                event=event,
                log_status="sent" if self.delivered else "pending",
                latest_feedback=self.feedback,
            )
            .model_dump_json()
            .encode()
        )


class FakeCloud:
    def __init__(self, api: FakeAPI, settings: LogSettings) -> None:
        self.api: FakeAPI = api
        self.settings: LogSettings = settings
        self.authenticated: bool = False
        self.encryption: str = "AES256"
        self.corrupt: bool = False

    def authenticate(self) -> None:
        self.authenticated = True

    def read(self, key: str) -> tuple[bytes, str | None]:
        assert self.authenticated
        event = next(event for event in self.api.events if object_key(self.settings, event) == key)
        return (b"{}" if self.corrupt else event.model_dump_json().encode(), self.encryption)


class MismatchedAPI(FakeAPI):
    def __init__(self, field: str) -> None:
        super().__init__()
        self.field: str = field

    @override
    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        data = super().request(path, timeout, body=body, headers=headers)
        if not path.startswith("/predictions/") or path.endswith("/feedback"):
            return data
        detail = PredictionDetail.model_validate_json(data)
        event = detail.event
        if self.field == "prediction_id":
            event = event.model_copy(update={"prediction_id": uuid4()})
        elif self.field == "recorded_at":
            event = event.model_copy(
                update={"recorded_at": event.recorded_at + timedelta(seconds=1)}
            )
        else:
            event = event.model_copy(
                update={
                    "response": event.response.model_copy(update={"workload_stage": "busy"}),
                }
            )
        return detail.model_copy(update={"event": event}).model_dump_json().encode()


def test_demo_records_historical_count_and_verifies_both_remote_objects() -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI()
    cloud = FakeCloud(api, settings)
    report = run_check(settings, api, cloud)
    assert report.status == "success"
    assert report.phase == "complete"
    assert report.authentication == "verified"
    assert report.actual_count == 0
    assert report.data_source == "demo"
    assert len(report.verified_keys) == 2
    assert "/prediction/" in report.verified_keys[0]
    assert "/feedback/" in report.verified_keys[1]


@pytest.mark.parametrize("destination", ["local", "local-s3"])
def test_emulated_mode_does_not_authenticate_or_submit(destination: LogBackend) -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI(destination)
    cloud = FakeCloud(api, settings)
    report = run_check(settings, api, cloud)
    assert report.status == "failed"
    assert report.error == "actual_s3_backend_required"
    assert not cloud.authenticated
    assert not api.events


def test_missing_truth_does_not_invent_feedback_or_submit(tmp_path: Path) -> None:
    truth = tmp_path / "missing.csv"
    _ = truth.write_text("target_hour,actual_count\n", encoding="utf-8")
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI()
    cloud = FakeCloud(api, settings)
    report = run_check(settings, api, cloud, truth=truth)
    assert report.phase == "historical_truth"
    assert report.error == "historical_truth_missing_or_ambiguous"
    assert not cloud.authenticated
    assert not api.events


@pytest.mark.parametrize("failure", ["encryption", "payload"])
def test_remote_verification_failure_cannot_report_success(failure: str) -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI()
    cloud = FakeCloud(api, settings)
    if failure == "encryption":
        cloud.encryption = "aws:kms"
    else:
        cloud.corrupt = True
    report = run_check(settings, api, cloud)
    assert report.status == "failed"
    assert report.phase == "s3_objects"
    assert report.error == (
        "s3_encryption_not_AES256" if failure == "encryption" else "s3_payload_mismatch"
    )


def test_error_report_never_contains_remote_exception_text() -> None:
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "SECRET"}}, "GetObject")
    assert safe_error(error) == "AccessDenied"
    assert safe_error(ValueError("SECRET")) == "ValueError"


def test_delivery_timeout_preserves_sanitized_worker_error() -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI()
    api.delivered = False
    report = run_check(settings, api, FakeCloud(api, settings), timeout=1)
    assert report.status == "failed"
    assert report.phase == "delivery"
    assert report.error == "timeout"
    assert report.delivery_error == "NoCredentialsError"


def test_last_object_finishing_after_deadline_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = FakeAPI()
    cloud = FakeCloud(api, settings)
    clock = [0.0]
    original_read = cloud.read

    def delayed_read(key: str) -> tuple[bytes, str | None]:
        result = original_read(key)
        if "/feedback/" in key:
            clock[0] = 2.0
        return result

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cloud, "read", delayed_read)
    report = run_check(settings, api, cloud, timeout=1)
    assert report.status == "failed"
    assert report.phase == "s3_objects"
    assert report.error == "timeout"


@pytest.mark.parametrize("field", ["response", "prediction_id", "recorded_at"])
def test_prediction_record_must_match_http_response(field: str) -> None:
    settings = LogSettings(bucket="unit-test-bucket")
    api = MismatchedAPI(field)
    report = run_check(settings, api, FakeCloud(api, settings))
    assert report.status == "failed"
    assert report.error == (
        "prediction_response_mismatch" if field == "response" else "prediction_record_mismatch"
    )
    assert report.verified_keys == []


def test_output_directory_failure_reports_failure_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WORKLOAD_LOG_BACKEND", "s3")
    monkeypatch.setenv("WORKLOAD_S3_BUCKET", "unit-test-bucket")
    monkeypatch.setattr(
        live_check,
        "run_check",
        Mock(
            return_value=CheckReport(
                status="success",
                phase="complete",
            )
        ),
    )
    with pytest.raises(typer.Exit) as caught:
        live_check.main(output=tmp_path)
    assert caught.value.exit_code == 1
    output = capsys.readouterr().out
    report = cast("dict[str, object]", json.loads(output))
    assert report["status"] == "failed"
    assert report["phase"] == "report_output"
    assert report["error"] in {"PermissionError", "IsADirectoryError"}
    assert '"status": "success"' not in output
    assert str(tmp_path) not in output
