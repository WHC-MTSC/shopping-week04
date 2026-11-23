from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workload.api import create_app
from workload.log_delivery import LogSettings
from workload.log_models import PredictionDetail
from workload.log_store import LogStore
from workload.replay_history import (
    SOURCE,
    TRUTH,
    ReplayError,
    ServerMetadata,
    read_examples,
    replay,
)


class OfflineAPI:
    def __init__(self, client: TestClient) -> None:
        self.client: TestClient = client
        self.paths: list[str] = []
        self.lose_feedback_response: bool = False
        self.model_sha_override: str | None = None
        self.detail_sha_override: str | None = None

    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        assert 1 <= timeout <= 30
        self.paths.append(path)
        response = self.client.request(
            "GET" if body is None else "POST",
            path,
            content=body,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        assert response.status_code == 200
        if path.endswith("/feedback") and self.lose_feedback_response:
            self.lose_feedback_response = False
            raise TimeoutError
        if path == "/metadata" and self.model_sha_override:
            metadata = ServerMetadata.model_validate_json(response.content)
            return (
                metadata.model_copy(update={"model_sha256": self.model_sha_override})
                .model_dump_json()
                .encode()
            )
        if path.startswith("/predictions/") and not path.endswith("/feedback"):
            replacement = self.detail_sha_override or self.model_sha_override
            if replacement:
                detail = PredictionDetail.model_validate_json(response.content)
                return (
                    detail.model_copy(
                        update={
                            "event": detail.event.model_copy(
                                update={"model_sha256": replacement},
                            )
                        }
                    )
                    .model_dump_json()
                    .encode()
                )
        return response.content


@pytest.fixture
def offline(tmp_path: Path) -> Iterator[tuple[OfflineAPI, LogStore]]:
    database = tmp_path / "events.db"
    with TestClient(
        create_app(Path("artifacts/model.json"), log_settings=LogSettings(database=database))
    ) as client:
        yield OfflineAPI(client), LogStore(database)


def test_real_csv_first_336_hours_have_verified_truth_and_causal_history() -> None:
    examples = read_examples(SOURCE.read_bytes(), TRUTH.read_bytes(), 336)
    assert len(examples) == 336
    assert examples[0].request.target_hour.isoformat() == "2011-09-27T06:00:00"
    assert examples[0].actual_count == 0
    assert examples[0].truth_row == 2
    assert examples[-1].truth_row == 337
    assert all(len(example.request.history) == 168 for example in examples)


def test_offline_replay_uses_demo_and_reruns_without_duplicate_events(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, store = offline
    first = replay(api, tmp_path / "first.json", limit=2)
    second = replay(api, tmp_path / "second.json", limit=2)
    assert first.status == second.status == "success"
    assert first.completed == second.completed == 2
    assert first.namespace == second.namespace
    assert first.items == second.items
    assert store.list_events().total == 4
    assert all(item.event.data_source == "demo" for item in store.list_events().items)
    assert first.source_sha256 is not None
    assert first.truth_sha256 is not None
    assert first.model_sha256 is not None
    assert first.source_history_row_start is not None
    assert first.source_target_row_end is not None
    assert first.source_history_row_start < first.source_target_row_end


def test_lost_feedback_response_is_resumed_with_same_deterministic_ids(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, store = offline
    api.lose_feedback_response = True
    failed = replay(api, tmp_path / "interrupted.json", limit=2)
    assert failed.status == "failed"
    assert failed.error == "TimeoutError"
    assert failed.completed == 0
    assert store.list_events().total == 2
    resumed = replay(api, tmp_path / "resumed.json", limit=2)
    assert resumed.status == "success"
    assert resumed.items[0].prediction_id == failed.items[0].prediction_id
    assert resumed.items[0].feedback_id == failed.items[0].feedback_id
    assert store.list_events().total == 4


def test_model_change_during_run_stops_before_attaching_truth(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, store = offline
    api.detail_sha_override = "f" * 64
    report = replay(api, tmp_path / "changed.json", limit=1)
    assert report.status == "failed"
    assert report.error == "serving_model_changed"
    assert store.list_events().total == 1
    assert not any(path.endswith("/feedback") for path in api.paths)


def test_unit_adapter_new_metadata_sha_uses_separate_id_namespace(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, store = offline
    first = replay(api, tmp_path / "model-one.json", limit=1)
    api.model_sha_override = "f" * 64
    second = replay(api, tmp_path / "model-two.json", limit=1)
    assert first.status == second.status == "success"
    assert first.namespace != second.namespace
    assert first.items[0].prediction_id != second.items[0].prediction_id
    assert store.list_events().total == 4


def test_existing_report_is_never_overwritten_or_submitted(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, _ = offline
    output = tmp_path / "existing.json"
    _ = output.write_text("keep", encoding="utf-8")
    with pytest.raises(ReplayError, match="output_must_be_new"):
        _ = replay(api, output, limit=1)
    assert output.read_text() == "keep"
    assert api.paths == []


def test_inconsistent_historical_truth_fails_before_any_http(
    offline: tuple[OfflineAPI, LogStore],
    tmp_path: Path,
) -> None:
    api, _ = offline
    truth = tmp_path / "wrong-truth.csv"
    _ = truth.write_text(
        "target_hour,actual_count,actual_stage\n2011-09-27 06:00:00,1,1\n", encoding="utf-8"
    )
    report = replay(api, tmp_path / "invalid.json", limit=1, truth=truth)
    assert report.status == "failed"
    assert report.error == "source_truth_count_mismatch"
    assert api.paths == []
