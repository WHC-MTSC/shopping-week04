"""Verify a localhost server's artifact identity and real prediction behavior."""

import hashlib
from http import HTTPStatus
from http.client import HTTPConnection, HTTPSConnection
from math import isclose
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import typer
from pydantic import BaseModel, ConfigDict

from workload.artifact import STAGE_RANGES, STAGES, ForestArtifact
from workload.features import feature_values
from workload.log_models import RecordedForecast
from workload.schemas import ForecastRequest, FrozenModel

MAX_RESPONSE_BYTES = 1024 * 1024


class ModelCheckError(ValueError):
    pass


class Metadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", strict=True)
    model_version: str
    model_sha256: str


class CheckReport(FrozenModel):
    status: Literal["passed", "failed"]
    error: str | None = None
    model_sha256: str | None = None
    prediction_id: UUID | None = None
    data_source: Literal["demo"] = "demo"


class JsonAPI(Protocol):
    def request(
        self, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> bytes: ...


class LocalAPI:
    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            message = "invalid_localhost_url"
            raise ModelCheckError(message)
        self.host: str = parts.hostname
        self.port: int | None = parts.port
        self.secure: bool = parts.scheme == "https"

    def request(
        self, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> bytes:
        connection_type = HTTPSConnection if self.secure else HTTPConnection
        connection = connection_type(self.host, self.port, timeout=5)
        try:
            connection.request(
                "POST" if body is not None else "GET",
                path,
                body=body,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            response = connection.getresponse()
            # http.client never follows redirects or forwards these bodies through a proxy.
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                message = f"HTTP_{response.status}"
                raise ModelCheckError(message)
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                message = "response_too_large"
                raise ModelCheckError(message)
            return payload
        finally:
            connection.close()


def verify(api: JsonAPI, model_path: Path) -> CheckReport:
    raw = model_path.read_bytes()
    artifact = ForestArtifact.model_validate_json(raw)
    digest = hashlib.sha256(raw).hexdigest()
    metadata = Metadata.model_validate_json(api.request("/metadata"))
    if metadata.model_version != artifact.model_version or metadata.model_sha256 != digest:
        return CheckReport(status="failed", error="metadata_model_mismatch", model_sha256=digest)
    sample = ForecastRequest.model_validate_json(api.request("/example"), strict=True)
    values = feature_values(sample.target_hour, [row.count for row in sample.history])
    expected = artifact.predict_proba(values)
    index = max(range(3), key=lambda item: expected[item])
    request_id = uuid4()
    actual = RecordedForecast.model_validate_json(
        api.request(
            "/predict",
            body=sample.model_dump_json().encode(),
            headers={"X-Data-Source": "demo", "Idempotency-Key": str(request_id)},
        ),
        strict=True,
    )
    matching = (
        actual.prediction_id == request_id
        and actual.model_version == artifact.model_version
        and actual.target_hour == sample.target_hour
        and actual.workload_stage == STAGES[index]
        and actual.stage_range == STAGE_RANGES[index]
        and all(
            isclose(observed, predicted, rel_tol=0, abs_tol=1e-10)
            for observed, predicted in zip(actual.probabilities, expected, strict=True)
        )
    )
    return CheckReport(
        status="passed" if matching else "failed",
        error=None if matching else "prediction_model_mismatch",
        model_sha256=digest,
        prediction_id=actual.prediction_id,
    )


def run_check(base_url: str, model_path: Path, *, api: JsonAPI | None = None) -> CheckReport:
    try:
        # Validate even when injecting a test transport.
        local = LocalAPI(base_url)
        return verify(api or local, model_path)
    except Exception as error:  # noqa: BLE001 -- Keep payloads and exception details out of JSON.
        code = str(error) if isinstance(error, ModelCheckError) else type(error).__name__
        if isinstance(error, HTTPError):
            code = f"HTTP_{error.code}"
        return CheckReport(status="failed", error=code)


def main(
    model: Annotated[
        Path, typer.Option(help="Artifact whose bytes must match the running server.")
    ],
    base_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8000",
) -> None:
    report = run_check(base_url, model)
    typer.echo(report.model_dump_json(indent=2))
    if report.status != "passed":
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
