import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from workload.api import create_app
from workload.log_delivery import LogSettings
from workload.log_models import (
    FeedbackReceipt,
    LoggingStatus,
    LogPage,
    PredictionDetail,
    RecordedForecast,
)
from workload.log_store import LogStore


@pytest.fixture
def settings(tmp_path: Path) -> LogSettings:
    return LogSettings(database=tmp_path / "events.sqlite3")


@pytest.fixture
def client(settings: LogSettings) -> Iterator[TestClient]:
    with TestClient(create_app(log_settings=settings)) as instance:
        yield instance


def predict(client: TestClient, request_id: UUID | None = None) -> RecordedForecast:
    response = client.post(
        "/predict",
        content=Path("examples/request.json").read_text(encoding="utf-8"),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(request_id or uuid4()),
            "X-Data-Source": "demo",
        },
    )
    assert response.status_code == 200
    return RecordedForecast.model_validate_json(response.text)


def test_prediction_records_input_features_output_and_model(client: TestClient) -> None:
    prediction = predict(client)
    detail = PredictionDetail.model_validate_json(
        client.get(f"/predictions/{prediction.prediction_id}").text
    )
    assert detail.log_status == "local"
    assert detail.event.prediction_id == prediction.prediction_id
    assert len(detail.event.request.history) == 168
    assert detail.event.response.workload_stage == prediction.workload_stage
    assert detail.event.response.probabilities == prediction.probabilities
    assert len(detail.event.features) == 18
    assert len(detail.event.model_sha256) == 64
    assert detail.event.data_source == "demo"
    assert detail.event.recorded_at.tzinfo is not None
    assert detail.latest_feedback is None


def test_predictions_and_feedback_survive_restart(settings: LogSettings) -> None:
    with TestClient(create_app(log_settings=settings)) as client:
        prediction = predict(client)
        response = client.post(
            f"/predictions/{prediction.prediction_id}/feedback",
            json={"submission_id": str(uuid4()), "feedback": {"kind": "actual_count", "value": 12}},
        )
        assert response.status_code == 200
    with TestClient(create_app(log_settings=settings)) as restarted:
        detail = PredictionDetail.model_validate_json(
            restarted.get(f"/predictions/{prediction.prediction_id}").text
        )
        assert detail.latest_feedback is not None
        assert detail.latest_feedback.event.actual_stage == "busy"
        assert detail.latest_feedback.event.is_correct is False


def test_retry_does_not_duplicate_and_changed_key_is_conflict(client: TestClient) -> None:
    request_id = uuid4()
    first = predict(client, request_id)
    assert predict(client, request_id) == first
    changed = (
        Path("examples/request.json")
        .read_text(encoding="utf-8")
        .replace('"count":  0', '"count":  1', 1)
    )
    response = client.post(
        "/predict",
        content=changed,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(request_id),
            "X-Data-Source": "demo",
        },
    )
    assert response.status_code == 409
    assert LoggingStatus.model_validate_json(client.get("/logging").text).pending_events == 1


@pytest.mark.parametrize(
    ("kind", "value", "stage", "correct"),
    [
        ("correctness", True, None, True),
        ("correctness", False, None, False),
        ("actual_count", 0, "idle", True),
        ("actual_count", 1, "normal", False),
        ("actual_count", 9, "normal", False),
        ("actual_count", 10, "busy", False),
        ("actual_stage", "busy", "busy", False),
        ("actual_stage", "idle", "idle", True),
    ],
)
def test_typed_feedback_preserves_semantics(
    client: TestClient, kind: str, stage: str | None, *, value: bool | int | str, correct: bool
) -> None:
    prediction = predict(client)
    response = client.post(
        f"/predictions/{prediction.prediction_id}/feedback",
        json={"submission_id": str(uuid4()), "feedback": {"kind": kind, "value": value}},
    )
    assert response.status_code == 200
    receipt = FeedbackReceipt.model_validate_json(response.text)
    assert receipt.event.actual_stage == stage
    assert receipt.event.is_correct is correct
    assert receipt.event.data_source == "demo"
    assert receipt.log_status == "local"


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("correctness", "false"),
        ("correctness", 0),
        ("actual_count", True),
        ("actual_count", "2"),
        ("actual_count", -1),
        ("actual_count", 1.5),
        ("actual_count", 100001),
        ("actual_stage", "unknown"),
        ("unexpected", True),
    ],
)
def test_invalid_feedback_does_not_create_events(
    client: TestClient, kind: str, *, value: bool | float | str
) -> None:
    prediction = predict(client)
    response = client.post(
        f"/predictions/{prediction.prediction_id}/feedback",
        json={"submission_id": str(uuid4()), "feedback": {"kind": kind, "value": value}},
    )
    assert response.status_code == 422
    assert LoggingStatus.model_validate_json(client.get("/logging").text).pending_events == 1


def test_feedback_retry_and_append_only_corrections(
    client: TestClient, settings: LogSettings
) -> None:
    prediction = predict(client)
    url = f"/predictions/{prediction.prediction_id}/feedback"
    submission_id = str(uuid4())
    payload = {"submission_id": submission_id, "feedback": {"kind": "actual_count", "value": 0}}
    first = client.post(url, json=payload)
    assert client.post(url, json=payload).text == first.text
    assert (
        client.post(
            url,
            json={
                "submission_id": submission_id,
                "feedback": {"kind": "actual_count", "value": 12},
            },
        ).status_code
        == 409
    )
    second = client.post(
        url, json={"submission_id": str(uuid4()), "feedback": {"kind": "actual_count", "value": 12}}
    )
    detail = PredictionDetail.model_validate_json(
        client.get(f"/predictions/{prediction.prediction_id}").text
    )
    assert detail.latest_feedback == FeedbackReceipt.model_validate_json(second.text)
    assert len(LogStore(settings.database).pending()) == 3


def test_unknown_prediction_and_extra_truth_fields_are_rejected(client: TestClient) -> None:
    assert client.get(f"/predictions/{uuid4()}").status_code == 404
    assert client.get("/predictions/not-an-id").status_code == 422
    assert (
        client.post(
            f"/predictions/{uuid4()}/feedback",
            json={
                "submission_id": str(uuid4()),
                "feedback": {"kind": "correctness", "value": True},
            },
        ).status_code
        == 404
    )
    prediction = predict(client)
    assert (
        client.post(
            f"/predictions/{prediction.prediction_id}/feedback",
            json={
                "submission_id": str(uuid4()),
                "feedback": {"kind": "correctness", "value": True, "actual_count": 2},
            },
        ).status_code
        == 422
    )


def test_failed_local_write_never_reports_saved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_self: LogStore, _event: object) -> None:
        raise sqlite3.OperationalError

    monkeypatch.setattr(LogStore, "save_prediction", unavailable)
    response = client.post(
        "/predict",
        content=Path("examples/request.json").read_text(encoding="utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 503


def test_log_console_lists_real_events_with_stable_keyset_pages(client: TestClient) -> None:
    first_prediction = predict(client)
    assert (
        client.post(
            f"/predictions/{first_prediction.prediction_id}/feedback",
            json={
                "submission_id": str(uuid4()),
                "feedback": {"kind": "correctness", "value": True},
            },
        ).status_code
        == 200
    )
    second_prediction = predict(client)
    first_page = LogPage.model_validate_json(client.get("/logs?limit=2").text)
    assert first_page.total == 3
    assert [item.event.event_type for item in first_page.items] == ["prediction", "feedback"]
    assert first_page.items[0].event.prediction_id == second_prediction.prediction_id
    assert first_page.next_cursor is not None
    _ = predict(client)  # Concurrent arrival cannot shift an older page or duplicate rows.
    next_page = LogPage.model_validate_json(
        client.get(f"/logs?limit=2&before={first_page.next_cursor}").text
    )
    assert next_page.total == 4
    assert len(next_page.items) == 1
    assert next_page.items[0].event.prediction_id == first_prediction.prediction_id
    assert next_page.items[0].sequence < first_page.next_cursor
    assert next_page.next_cursor is None


def test_log_console_combines_filters_and_treats_search_as_literal(client: TestClient) -> None:
    prediction = predict(client)
    assert (
        client.post(
            f"/predictions/{prediction.prediction_id}/feedback",
            json={"submission_id": str(uuid4()), "feedback": {"kind": "actual_count", "value": 12}},
        ).status_code
        == 200
    )
    _ = predict(client)
    page = LogPage.model_validate_json(
        client.get(
            "/logs",
            params={"prediction_id": str(prediction.prediction_id), "event_type": "feedback"},
        ).text
    )
    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0].event.event_type == "feedback"
    assert page.items[0].log_status == "local"
    searched = LogPage.model_validate_json(
        client.get("/logs", params={"query": str(prediction.prediction_id)}).text
    )
    assert searched.total == 2
    for literal in ("%' OR 1=1 --", "%", "_"):
        # Underscores occur in JSON field names, so compare them to a direct body substring.
        result = LogPage.model_validate_json(client.get("/logs", params={"query": literal}).text)
        assert all(literal in item.event.model_dump_json() for item in result.items)
    assert LogPage.model_validate_json(client.get("/logs?before=1").text).items == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "before=0",
        "event_type=invalid",
        "prediction_id=bad",
        "query=" + "x" * 129,
    ],
)
def test_log_console_rejects_invalid_queries(client: TestClient, query: str) -> None:
    assert client.get(f"/logs?{query}").status_code == 422
