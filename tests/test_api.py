from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workload.api import create_app
from workload.artifact import ForestArtifact, TreeArtifact
from workload.features import FEATURE_NAMES
from workload.schemas import ForecastRequest, ForecastResponse, HealthResponse, Observation


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    leaf = TreeArtifact(
        left=[-1], right=[-1], feature=[-1], threshold=[-2], probabilities=[(0.2, 0.3, 0.5)]
    )
    model = ForestArtifact(model_version="test", feature_names=FEATURE_NAMES, trees=[leaf])
    path = tmp_path / "model.json"
    _ = path.write_text(model.model_dump_json(), encoding="utf-8")
    with TestClient(create_app(path)) as test_client:
        yield test_client


@pytest.fixture
def payload() -> ForecastRequest:
    target = datetime(2011, 9, 27, 6)
    return ForecastRequest(
        target_hour=target,
        history=[
            Observation(hour=target - timedelta(hours=168 - i), count=i % 20) for i in range(168)
        ],
    )


def test_predict_when_valid_history_is_posted(client: TestClient, payload: ForecastRequest) -> None:
    response = client.post(
        "/predict", content=payload.model_dump_json(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    result = ForecastResponse.model_validate_json(response.text)
    assert result.workload_stage == "busy"
    assert result.probabilities == pytest.approx([0.2, 0.3, 0.5])


def test_health_when_model_has_loaded(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert HealthResponse.model_validate_json(response.text) == HealthResponse(
        status="ready", model_version="test"
    )


@pytest.mark.parametrize("endpoint", ["/metadata", "/openapi.json"])
def test_documentation_when_requested(client: TestClient, endpoint: str) -> None:
    assert client.get(endpoint).status_code == 200


@pytest.mark.parametrize(
    "bad_json",
    [
        "{}",
        '{"target_hour":"2011-09-27T06:00:00Z","history":[]}',
        '{"target_hour":"2011-09-27T06:15:00","history":[]}',
        '{"target_hour":"2011-09-27T06:00:00","history":[],"label":10}',
    ],
)
def test_predict_when_invalid_input_is_posted(client: TestClient, bad_json: str) -> None:
    assert (
        client.post(
            "/predict", content=bad_json, headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )


def test_startup_when_model_is_missing_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError), TestClient(create_app(tmp_path / "missing.json")):
        pass


@pytest.mark.parametrize("count", ["NaN", "Infinity", "-Infinity", "1e1000"])
def test_predict_when_nonfinite_count_is_posted_returns_422(
    client: TestClient,
    payload: ForecastRequest,
    count: str,
) -> None:
    raw = payload.model_dump_json().replace('"count":0', f'"count":{count}', 1)
    response = client.post("/predict", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_predict_when_target_cannot_have_prior_history_returns_422(
    client: TestClient,
    payload: ForecastRequest,
) -> None:
    raw = payload.model_dump_json().replace("2011-09-27T06:00:00", "0001-01-01T00:00:00", 1)
    response = client.post("/predict", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
