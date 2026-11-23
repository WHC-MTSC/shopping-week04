"""Replay verified historical examples as demo activity on the isolated port 8002 app."""

import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, ClassVar, TextIO
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import typer
from pydantic import BaseModel, ConfigDict, Field

from workload.artifact import STAGES
from workload.features import HISTORY_HOURS, label_count
from workload.live_check import HTTPAPI, JsonAPI, safe_error, verify_prediction_response
from workload.log_models import (
    CountFeedback,
    FeedbackReceipt,
    FeedbackRequest,
    PredictionDetail,
    RecordedForecast,
)
from workload.schemas import ForecastRequest, Observation

SOURCE = Path("data/model-input.csv")
TRUTH = Path("artifacts/test-predictions.csv")
BASE_URL = "http://127.0.0.1:8001"
MAX_LIMIT = 500
MAX_REQUEST_TIMEOUT = 30


class ReplayError(ValueError):
    pass


def require(condition: bool, code: str) -> None:  # noqa: FBT001 -- Named validation predicate.
    if not condition:
        raise ReplayError(code)


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    require(
        parts.scheme == "http"
        and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        and parts.username is None
        and parts.password is None
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment,
        "base_url_must_be_local_http",
    )
    _ = parts.port
    return value.rstrip("/")


class ServerMetadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)
    model_version: str
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass
class HistoricalExample:
    request: ForecastRequest
    actual_count: int
    source_row: int
    truth_row: int


@dataclass
class ReplayItem:
    target_hour: str
    source_row: int
    truth_row: int
    prediction_id: str
    feedback_id: str
    actual_count: int
    status: str = "planned"
    model_version: str | None = None
    prediction_log_status: str | None = None
    feedback_log_status: str | None = None


@dataclass
class ReplayReport:
    status: str = "running"
    phase: str = "source_validation"
    error: str | None = None
    purpose: str = "historical_replay_practice"
    data_source: str = "demo"
    note: str = "Historical dataset practice, not live operations or team activity."
    base_url: str = BASE_URL
    run_seed: str = "historical-practice-v1"
    namespace: str | None = None
    source_sha256: str | None = None
    truth_sha256: str | None = None
    source_history_row_start: int | None = None
    source_target_row_end: int | None = None
    truth_row_start: int | None = None
    truth_row_end: int | None = None
    model_version: str | None = None
    model_sha256: str | None = None
    requested: int = 0
    completed: int = 0
    items: list[ReplayItem] = field(default_factory=list)


def read_examples(source: bytes, truth: bytes, limit: int) -> list[HistoricalExample]:
    raw = list(csv.DictReader(io.StringIO(source.decode("utf-8-sig"))))
    observations = [
        Observation(
            hour=datetime.fromisoformat(row["target_hour"]),
            count=int(row["positive_sales_invoice_count"]),
        )
        for row in raw
    ]
    positions = {row.hour: index for index, row in enumerate(observations)}
    require(len(positions) == len(observations), "duplicate_source_hour")
    truths = list(csv.DictReader(io.StringIO(truth.decode("utf-8-sig"))))
    require(len(truths) >= limit, "not_enough_verified_targets")
    examples: list[HistoricalExample] = []
    for index, row in enumerate(truths[:limit]):
        target = datetime.fromisoformat(row["target_hour"])
        if examples:
            require(
                target - examples[-1].request.target_hour == timedelta(hours=1),
                "truth_hours_not_consecutive",
            )
        require(target in positions, "truth_target_missing_from_source")
        position = positions[target]
        require(position >= HISTORY_HOURS, "insufficient_source_history")
        actual = CountFeedback(kind="actual_count", value=int(row["actual_count"])).value
        require(actual == observations[position].count, "source_truth_count_mismatch")
        require(int(row["actual_stage"]) == label_count(actual), "truth_stage_mismatch")
        request = ForecastRequest(
            target_hour=target, history=observations[position - HISTORY_HOURS : position]
        )
        examples.append(HistoricalExample(request, actual, position + 2, index + 2))
    return examples


def checkpoint(destination: TextIO, report: ReplayReport) -> None:
    _ = destination.seek(0)
    _ = destination.write(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
    _ = destination.truncate()
    destination.flush()


def submit_example(
    api: JsonAPI,
    example: HistoricalExample,
    item: ReplayItem,
    metadata: ServerMetadata,
    timeout: float,
) -> None:
    forecast = RecordedForecast.model_validate_json(
        api.request(
            "/predict",
            timeout,
            body=example.request.model_dump_json().encode(),
            headers={"Idempotency-Key": item.prediction_id, "X-Data-Source": "demo"},
        )
    )
    require(str(forecast.prediction_id) == item.prediction_id, "prediction_id_mismatch")
    require(forecast.model_version == metadata.model_version, "serving_model_changed")
    detail = PredictionDetail.model_validate_json(
        api.request(f"/predictions/{item.prediction_id}", timeout)
    )
    require(detail.event.model_sha256 == metadata.model_sha256, "serving_model_changed")
    require(
        detail.event.request == example.request and detail.event.data_source == "demo",
        "stored_prediction_mismatch",
    )
    verify_prediction_response(forecast, detail)
    item.status = "prediction_recorded"
    item.model_version = forecast.model_version
    item.prediction_log_status = detail.log_status
    submission = FeedbackRequest(
        submission_id=UUID(item.feedback_id),
        feedback=CountFeedback(kind="actual_count", value=example.actual_count),
    )
    receipt = FeedbackReceipt.model_validate_json(
        api.request(
            f"/predictions/{item.prediction_id}/feedback",
            timeout,
            body=submission.model_dump_json().encode(),
        )
    )
    expected_stage = STAGES[label_count(example.actual_count)]
    require(
        receipt.event.submission == submission
        and receipt.event.event_id == submission.submission_id
        and str(receipt.event.prediction_id) == item.prediction_id
        and receipt.event.data_source == "demo"
        and receipt.event.actual_stage == expected_stage
        and receipt.event.is_correct == (forecast.workload_stage == expected_stage),
        "stored_feedback_mismatch",
    )
    item.feedback_log_status = receipt.log_status
    item.status = "complete"


def replay(  # noqa: PLR0913 -- Explicit input files and injectable HTTP client support offline verification.
    api: JsonAPI,
    output: Path,
    *,
    limit: int = 336,
    run_seed: str = "historical-practice-v1",
    source: Path = SOURCE,
    truth: Path = TRUTH,
    request_timeout: float = 5,
    base_url: str = BASE_URL,
) -> ReplayReport:
    require(not output.exists() and not output.is_symlink(), "output_must_be_new")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = ReplayReport(run_seed=run_seed, requested=limit, base_url=validate_base_url(base_url))
    with output.open("x", encoding="utf-8") as destination:
        checkpoint(destination, report)
        try:
            require(1 <= limit <= MAX_LIMIT, "limit_must_be_1_to_500")
            require(1 <= request_timeout <= MAX_REQUEST_TIMEOUT, "invalid_request_timeout")
            require(bool(run_seed.strip()), "run_seed_must_not_be_empty")
            source_bytes, truth_bytes = source.read_bytes(), truth.read_bytes()
            report.source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            report.truth_sha256 = hashlib.sha256(truth_bytes).hexdigest()
            examples = read_examples(source_bytes, truth_bytes, limit)
            report.source_history_row_start = examples[0].source_row - HISTORY_HOURS
            report.source_target_row_end = examples[-1].source_row
            report.truth_row_start, report.truth_row_end = (
                examples[0].truth_row,
                examples[-1].truth_row,
            )
            report.phase = "metadata"
            metadata = ServerMetadata.model_validate_json(api.request("/metadata", request_timeout))
            report.model_version, report.model_sha256 = (
                metadata.model_version,
                metadata.model_sha256,
            )
            namespace = uuid5(
                NAMESPACE_URL,
                f"{run_seed}:{report.source_sha256}:{report.truth_sha256}:{metadata.model_sha256}",
            )
            report.namespace = str(namespace)
            report.phase = "replay"
            for example in examples:
                hour = example.request.target_hour.isoformat()
                item = ReplayItem(
                    hour,
                    example.source_row,
                    example.truth_row,
                    str(uuid5(namespace, "prediction:" + hour)),
                    str(uuid5(namespace, "feedback:" + hour)),
                    example.actual_count,
                )
                report.items.append(item)
                checkpoint(destination, report)
                submit_example(api, example, item, metadata, request_timeout)
                report.completed += 1
                checkpoint(destination, report)
            report.status, report.phase = "success", "complete"
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- Preserve resumable IDs and redact failures.
            report.status = "failed"
            report.error = (
                str(error)
                if isinstance(error, ReplayError)
                else safe_error(error)
                if isinstance(error, Exception)
                else type(error).__name__
            )
        checkpoint(destination, report)
    return report


def main(
    output: Annotated[Path, typer.Option()],
    limit: Annotated[int, typer.Option(min=1, max=MAX_LIMIT)] = 336,
    run_seed: Annotated[str, typer.Option()] = "historical-practice-v1",
    request_timeout: Annotated[float, typer.Option(min=1, max=MAX_REQUEST_TIMEOUT)] = 5,
    base_url: Annotated[str, typer.Option()] = BASE_URL,
) -> None:
    try:
        report = replay(
            HTTPAPI(validate_base_url(base_url)),
            output,
            limit=limit,
            run_seed=run_seed,
            request_timeout=request_timeout,
            base_url=base_url,
        )
    except Exception as error:  # noqa: BLE001 -- Never output HTTP response bodies or credentials.
        typer.echo(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(error) if isinstance(error, ReplayError) else safe_error(error),
                }
            )
        )
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "status": report.status,
                "completed": report.completed,
                "report": str(output),
                "error": report.error,
            }
        )
    )
    if report.status != "success":
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
