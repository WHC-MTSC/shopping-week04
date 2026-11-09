import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import typer
from typer.testing import CliRunner

from workload import model_check
from workload.artifact import ForestArtifact, TreeArtifact
from workload.features import FEATURE_NAMES
from workload.log_models import RecordedForecast
from workload.schemas import ForecastRequest


@pytest.fixture
def model_path(tmp_path: Path) -> Path:
    artifact = ForestArtifact(
        model_version="candidate-v2",
        feature_names=FEATURE_NAMES,
        trees=[
            TreeArtifact(
                left=[-1], right=[-1], feature=[-2], threshold=[-2], probabilities=[(0.2, 0.3, 0.5)]
            )
        ],
    )
    path = tmp_path / "model.json"
    _ = path.write_text(artifact.model_dump_json(), encoding="utf-8")
    return path


class FakeAPI:
    def __init__(self, path: Path, mismatch: str = "") -> None:
        self.digest: str = hashlib.sha256(path.read_bytes()).hexdigest()
        self.mismatch: str = mismatch
        self.paths: list[str] = []

    def request(
        self, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> bytes:
        self.paths.append(path)
        if path == "/metadata":
            return json.dumps(
                {
                    "model_version": "old-version"
                    if self.mismatch == "metadata_version"
                    else "candidate-v2",
                    "model_sha256": "0" * 64 if self.mismatch == "sha256" else self.digest,
                    "unrelated_field": True,
                }
            ).encode()
        if path == "/example":
            return Path("examples/request.json").read_bytes()
        assert path == "/predict"
        assert body is not None
        assert headers is not None
        assert headers["X-Data-Source"] == "demo"
        sample = ForecastRequest.model_validate_json(body)
        forecast = RecordedForecast(
            target_hour=sample.target_hour,
            workload_stage="idle" if self.mismatch == "stage" else "busy",
            stage_range="10+",
            probabilities=(0.5, 0.3, 0.2) if self.mismatch == "probabilities" else (0.2, 0.3, 0.5),
            model_version="old-version"
            if self.mismatch == "prediction_version"
            else "candidate-v2",
            prediction_id=UUID(headers["Idempotency-Key"]),
            recorded_at=datetime.now(UTC),
            log_status="local",
        )
        return forecast.model_dump_json().encode()


def test_matches_digest_version_and_real_demo_prediction(model_path: Path) -> None:
    api = FakeAPI(model_path)
    result = model_check.run_check("http://localhost:8000", model_path, api=api)
    assert result.status == "passed"
    assert result.prediction_id is not None
    assert result.model_sha256 == api.digest
    assert api.paths == ["/metadata", "/example", "/predict"]


@pytest.mark.parametrize("mismatch", ["sha256", "metadata_version"])
def test_metadata_mismatch_stops_before_logging_prediction(model_path: Path, mismatch: str) -> None:
    api = FakeAPI(model_path, mismatch)
    result = model_check.run_check("http://127.0.0.1:8000", model_path, api=api)
    assert result.status == "failed"
    assert result.error == "metadata_model_mismatch"
    assert api.paths == ["/metadata"]


@pytest.mark.parametrize("mismatch", ["stage", "probabilities", "prediction_version"])
def test_prediction_mismatch_fails(model_path: Path, mismatch: str) -> None:
    result = model_check.run_check(
        "http://127.0.0.1:8000", model_path, api=FakeAPI(model_path, mismatch)
    )
    assert result.status == "failed"
    assert result.error == "prediction_model_mismatch"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost.evil.test",
        "http://user:private@localhost",
        "http://localhost/path",
        "file:///tmp/model",
        "http://localhost?token=private",
    ],
)
def test_nonlocal_or_credential_bearing_url_rejected_before_requests(
    model_path: Path, url: str
) -> None:
    api = FakeAPI(model_path)
    result = model_check.run_check(url, model_path, api=api)
    assert result.status == "failed"
    assert result.error == "invalid_localhost_url"
    assert api.paths == []


def test_safe_error_json_and_nonzero_cli_exit(
    model_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_request(
        _self: model_check.LocalAPI,
        _path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        del body, headers
        message = "private payload secret"
        raise ConnectionError(message)

    monkeypatch.setattr(model_check.LocalAPI, "request", failing_request)
    app = typer.Typer()
    _ = app.command()(model_check.main)
    result = CliRunner().invoke(app, ["--model", str(model_path)])
    assert result.exit_code == 1
    report = model_check.CheckReport.model_validate_json(result.output)
    assert report.error == "ConnectionError"
    assert "private" not in result.output


def test_missing_model_is_safe_failure(tmp_path: Path) -> None:
    result = model_check.run_check("http://127.0.0.1:8000", tmp_path / "missing.json")
    assert result.status == "failed"
    assert result.error == "FileNotFoundError"
