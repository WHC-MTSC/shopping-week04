from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workload.api import MetadataResponse, create_app
from workload.log_delivery import LogSettings
from workload.log_models import RecordedForecast
from workload.schemas import ForecastRequest, ForecastResponse


def test_dashboard_sample_flows_through_real_model(tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        sample = client.get("/example")
        assert sample.status_code == 200
        request = ForecastRequest.model_validate_json(sample.text)
        response = client.post(
            "/predict",
            content=request.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        actual = RecordedForecast.model_validate_json(response.text)
        expected = ForecastResponse.model_validate_json(
            Path("examples/response.json").read_text(encoding="utf-8")
        )
        assert actual.model_version == expected.model_version
        assert actual.target_hour == expected.target_hour
        assert actual.workload_stage == expected.workload_stage
        assert actual.stage_range == expected.stage_range
        assert actual.probabilities == pytest.approx(expected.probabilities, abs=1e-12)
        metadata = MetadataResponse.model_validate_json(client.get("/metadata").text)
        assert metadata.history_hours == len(request.history)
        assert metadata.logging_enabled is True


@pytest.mark.parametrize(
    ("url", "media_type"),
    [
        ("/", "text/html"),
        ("/log-console", "text/html"),
        ("/static/styles.css", "text/css"),
        ("/static/logs.css", "text/css"),
        ("/static/app.mjs", "javascript"),
        ("/static/input.mjs", "javascript"),
        ("/static/feedback.mjs", "javascript"),
        ("/static/logs.mjs", "javascript"),
    ],
)
def test_dashboard_assets_are_browser_loadable(url: str, media_type: str, tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert media_type in response.headers["content-type"]


def test_dashboard_does_not_expose_repository_files(tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        assert client.get("/static/%2e%2e/artifact.py").status_code == 404
        assert client.get("/artifacts/model.json").status_code == 404
