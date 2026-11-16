"""Synthetic API-to-dataset integration; CloudWatch uses an in-memory SDK double."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Literal, cast
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from workload.api import MetadataResponse, create_app
from workload.artifact import ForestArtifact, TreeArtifact
from workload.cloudwatch_delivery import CloudWatchSink
from workload.export_logs import export_logs
from workload.features import FEATURE_NAMES
from workload.log_delivery import LogDelivery, LogSettings
from workload.log_models import (
    CountFeedback,
    Event,
    FeedbackReceipt,
    FeedbackRequest,
    LoggingStatus,
    PredictionDetail,
    RecordedForecast,
)
from workload.log_store import LogStore
from workload.prepare_logs import RewardRow, SupervisedRow, prepare
from workload.schemas import ForecastRequest, Observation


class SyntheticCloudWatchSDK:
    """Record requests locally and emulate an unavailable AWS endpoint."""

    def __init__(self) -> None:
        self.offline: bool = True
        self.accepted: list[str] = []
        self.lock: Lock = Lock()

    def put_log_events(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["logGroupName"] == "/synthetic/integration-only"
        assert kwargs["logStreamName"] == "synthetic-test-only"
        with self.lock:
            if self.offline:
                raise EndpointConnectionError(endpoint_url="https://synthetic.invalid")
            records = cast("list[dict[str, object]]", kwargs["logEvents"])
            self.accepted.extend(cast("str", record["message"]) for record in records)
        return {}


@dataclass
class SyntheticFixture:
    settings: LogSettings
    model_path: Path
    model_sha256: str
    request: ForecastRequest


@pytest.fixture
def synthetic_fixture(tmp_path: Path) -> SyntheticFixture:
    model = ForestArtifact(
        model_version="synthetic-integration-only",
        feature_names=FEATURE_NAMES,
        trees=[
            TreeArtifact(
                left=[-1], right=[-1], feature=[-1], threshold=[-2], probabilities=[(0.2, 0.6, 0.2)]
            )
        ],
    )
    model_path = tmp_path / "synthetic-model.json"
    body = model.model_dump_json().encode()
    _ = model_path.write_bytes(body)
    target = datetime(2025, 1, 8, 12)
    request = ForecastRequest(
        target_hour=target,
        history=[
            Observation(hour=target - timedelta(hours=168 - index), count=index % 7)
            for index in range(168)
        ],
    )
    return SyntheticFixture(
        settings=LogSettings(
            database=tmp_path / "synthetic-events.db",
            backend="cloudwatch",
            log_group="/synthetic/integration-only",
            log_stream="synthetic-test-only",
        ),
        model_path=model_path,
        model_sha256=hashlib.sha256(body).hexdigest(),
        request=request,
    )


def record_synthetic_prediction_and_answer(
    fixture: SyntheticFixture,
    sdk: SyntheticCloudWatchSDK,
    source: Literal["user", "demo"],
) -> PredictionDetail:
    sink = CloudWatchSink(fixture.settings, sdk)
    with TestClient(
        create_app(fixture.model_path, log_settings=fixture.settings, log_sink=sink)
    ) as client:
        metadata = MetadataResponse.model_validate_json(client.get("/metadata").text)
        assert metadata.model_sha256 == fixture.model_sha256
        identifier = str(uuid4())
        headers = {
            "Idempotency-Key": identifier,
            "X-Data-Source": source,
            "Content-Type": "application/json",
        }
        first = client.post("/predict", content=fixture.request.model_dump_json(), headers=headers)
        retry = client.post("/predict", content=fixture.request.model_dump_json(), headers=headers)
        assert first.status_code == retry.status_code == 200
        forecast = RecordedForecast.model_validate_json(first.text)
        assert RecordedForecast.model_validate_json(retry.text) == forecast
        # This value is fabricated solely inside the test, never collected as real user truth.
        feedback = FeedbackRequest(
            submission_id=uuid4(), feedback=CountFeedback(kind="actual_count", value=12)
        )
        path = f"/predictions/{identifier}/feedback"
        receipt = client.post(
            path, content=feedback.model_dump_json(), headers={"Content-Type": "application/json"}
        )
        receipt_retry = client.post(
            path, content=feedback.model_dump_json(), headers={"Content-Type": "application/json"}
        )
        assert receipt.status_code == receipt_retry.status_code == 200
        assert FeedbackReceipt.model_validate_json(
            receipt.text
        ) == FeedbackReceipt.model_validate_json(receipt_retry.text)
        detail = PredictionDetail.model_validate_json(client.get(f"/predictions/{identifier}").text)
        assert detail.event.model_sha256 == metadata.model_sha256
        assert detail.event.response.model_version == metadata.model_version
        assert detail.event.data_source == source
    assert detail.latest_feedback is not None
    assert detail.latest_feedback.event.data_source == source
    return detail


def retry_and_restart_synthetic_delivery(
    fixture: SyntheticFixture,
    sdk: SyntheticCloudWatchSDK,
    detail: PredictionDetail,
) -> None:
    store = LogStore(fixture.settings.database, cloudwatch=True)
    store.bind_cloudwatch_destination(
        fixture.settings.region, "/synthetic/integration-only", "synthetic-test-only"
    )
    sink = CloudWatchSink(fixture.settings, sdk)
    assert LogDelivery(store, sink).flush() == 0
    assert len(store.pending()) == 2
    assert store.status(fixture.settings.region).last_error == "EndpointConnectionError"
    sdk.offline = False
    restarted_store = LogStore(fixture.settings.database, cloudwatch=True)
    assert LogDelivery(restarted_store, sink).flush() == 2
    assert restarted_store.pending() == []
    with TestClient(
        create_app(fixture.model_path, log_settings=fixture.settings, log_sink=sink)
    ) as client:
        saved = PredictionDetail.model_validate_json(
            client.get(f"/predictions/{detail.event.prediction_id}").text
        )
        assert saved.event == detail.event
        assert saved.log_status == "cloudwatch_sent"
        assert saved.latest_feedback is not None
        assert saved.latest_feedback.log_status == "cloudwatch_sent"
        status = LoggingStatus.model_validate_json(client.get("/logging").text)
        assert status.destination == "cloudwatch"
        assert status.log_group == fixture.settings.log_group
        assert status.log_stream == fixture.settings.log_stream
        assert status.sent_events == 2


@pytest.mark.parametrize("source", ["demo", "user"])
def test_synthetic_api_mock_cloudwatch_restart_export_and_prepare(
    synthetic_fixture: SyntheticFixture,
    tmp_path: Path,
    source: Literal["user", "demo"],
) -> None:
    sdk = SyntheticCloudWatchSDK()
    detail = record_synthetic_prediction_and_answer(synthetic_fixture, sdk, source)
    retry_and_restart_synthetic_delivery(synthetic_fixture, sdk, detail)
    output = tmp_path / "exported"
    manifest = export_logs(
        source="local", database=synthetic_fixture.settings.database, output=output
    )
    assert manifest["count"] == 2
    body = (output / "events.jsonl").read_bytes()
    assert manifest["sha256"] == hashlib.sha256(body).hexdigest()
    exported = [TypeAdapter(Event).validate_json(line) for line in body.splitlines()]
    delivered = [TypeAdapter(Event).validate_json(message) for message in sdk.accepted]
    assert exported == delivered
    prepared = tmp_path / "prepared"
    summary = prepare(output / "events.jsonl", prepared)
    assert summary.counts["quarantined_records"] == 0
    if source == "demo":
        assert summary.counts["excluded_demo_events"] == 2
        assert summary.counts["supervised_rows"] == summary.counts["reward_rows"] == 0
        assert (prepared / "supervised.jsonl").read_bytes() == b""
        demo = prepare(output / "events.jsonl", tmp_path / "demo-only", include_demo=True)
        assert demo.counts["supervised_rows"] == 1
        assert not demo.promotion_eligible
    else:
        assert summary.counts["supervised_rows"] == summary.counts["reward_rows"] == 1
        row = SupervisedRow.model_validate_json((prepared / "supervised.jsonl").read_bytes())
        reward = RewardRow.model_validate_json((prepared / "rewards.jsonl").read_bytes())
        assert row.label == 2
        assert row.features == [detail.event.features[name] for name in FEATURE_NAMES]
        assert row.prediction_id == detail.event.prediction_id
        assert row.model_version == "synthetic-integration-only"
        assert reward.action == "normal"
        assert reward.reward == 0
        assert reward.feedback_kind == "actual_count"
