"""Verify a demo prediction and historical truth through HTTP and actual AWS S3."""

import csv
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
import typer
from botocore.config import Config
from botocore.exceptions import ClientError

from workload.log_delivery import LogSettings, object_key
from workload.log_models import (
    CountFeedback,
    Event,
    FeedbackReceipt,
    FeedbackRequest,
    LoggingStatus,
    PredictionDetail,
    RecordedForecast,
)
from workload.schemas import ForecastRequest

if TYPE_CHECKING:
    from http.client import HTTPResponse

    from mypy_boto3_s3.client import S3Client


class CheckError(RuntimeError):
    pass


def fail(code: str) -> NoReturn:
    raise CheckError(code)


EXAMPLE = Path("examples/request.json")
TRUTH = Path("artifacts/test-predictions.csv")
MAX_TIMEOUT = 300


class JsonAPI(Protocol):
    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes: ...


class Cloud(Protocol):
    def authenticate(self) -> None: ...

    def read(self, key: str) -> tuple[bytes, str | None]: ...


class IdentityClient(Protocol):
    def get_caller_identity(self) -> object: ...


class HTTPAPI:
    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            fail("invalid_base_url")
        self.base_url: str = base_url.rstrip("/")

    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request = Request(  # noqa: S310 -- Scheme is restricted to HTTP(S) in __init__.
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if body is not None else "GET",
        )
        with cast("HTTPResponse", urlopen(request, timeout=timeout)) as response:  # noqa: S310
            return response.read()


class AWSCloud:
    def __init__(self, settings: LogSettings) -> None:
        self.settings: LogSettings = settings
        self.client: S3Client | None = None

    def authenticate(self) -> None:
        session = boto3.Session(
            profile_name=self.settings.profile, region_name=self.settings.region
        )
        config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
        identity = cast(
            "IdentityClient",
            session.client(  # pyright: ignore[reportUnknownMemberType]
                "sts", config=config
            ),
        )
        _ = identity.get_caller_identity()
        self.client = session.client("s3", config=config)  # pyright: ignore[reportUnknownMemberType]

    def read(self, key: str) -> tuple[bytes, str | None]:
        if self.client is None or self.settings.bucket is None:
            fail("aws_not_authenticated")
        result = self.client.get_object(Bucket=self.settings.bucket, Key=key)
        stream = result["Body"]
        try:
            return stream.read(), result.get("ServerSideEncryption")
        finally:
            stream.close()


@dataclass
class CheckReport:
    status: str = "failed"
    phase: str = "preflight"
    error: str | None = None
    delivery_error: str | None = None
    data_source: str = "demo"
    authentication: str = "not_checked"
    region: str = ""
    bucket: str | None = None
    prediction_id: str | None = None
    feedback_id: str | None = None
    actual_count: int | None = None
    verified_keys: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0


def historical_truth(path: Path, sample: ForecastRequest) -> int:
    with path.open(encoding="utf-8", newline="") as source:
        matches = [
            row["actual_count"]
            for row in csv.DictReader(source)
            if datetime.fromisoformat(row["target_hour"]) == sample.target_hour
        ]
    if len(matches) != 1:
        fail("historical_truth_missing_or_ambiguous")
    return CountFeedback(kind="actual_count", value=int(matches[0])).value


def safe_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP_{error.code}"
    if isinstance(error, CheckError):
        return str(error)
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            return code
    return type(error).__name__


def verify_object(cloud: Cloud, settings: LogSettings, event: Event) -> str:
    key = object_key(settings, event)
    body, encryption = cloud.read(key)
    if encryption != "AES256":
        fail("s3_encryption_not_AES256")
    if json.dumps(json.loads(body), sort_keys=True) != json.dumps(
        json.loads(event.model_dump_json()), sort_keys=True
    ):
        fail("s3_payload_mismatch")
    return key


def verify_prediction_response(forecast: RecordedForecast, detail: PredictionDetail) -> None:
    if (
        detail.event.prediction_id != forecast.prediction_id
        or detail.event.recorded_at != forecast.recorded_at
    ):
        fail("prediction_record_mismatch")
    response = forecast.model_dump(exclude={"prediction_id", "recorded_at", "log_status"})
    if response != detail.event.response.model_dump():
        fail("prediction_response_mismatch")


def run_check(  # noqa: C901, PLR0913 -- Ordered phases with injectable offline dependencies.
    settings: LogSettings,
    api: JsonAPI,
    cloud: Cloud,
    *,
    timeout: float = 60,
    example: Path = EXAMPLE,
    truth: Path = TRUTH,
) -> CheckReport:
    report = CheckReport(region=settings.region, bucket=settings.bucket)
    started = time.monotonic()

    def remaining() -> float:
        seconds = timeout - (time.monotonic() - started)
        if seconds <= 0:
            fail("timeout")
        return min(seconds, 5)

    try:
        if not 1 <= timeout <= MAX_TIMEOUT:
            fail("timeout_must_be_1_to_300_seconds")
        status = LoggingStatus.model_validate_json(api.request("/logging", remaining()))
        if status.destination != "s3" or settings.backend != "s3":
            fail("actual_s3_backend_required")
        if status.region != settings.region:
            fail("region_mismatch")
        report.phase = "historical_truth"
        sample = ForecastRequest.model_validate_json(example.read_bytes())
        report.actual_count = historical_truth(truth, sample)
        report.phase = "authentication"
        cloud.authenticate()
        report.authentication = "verified"
        report.phase = "prediction"
        prediction_id = uuid4()
        report.prediction_id = str(prediction_id)
        forecast = RecordedForecast.model_validate_json(
            api.request(
                "/predict",
                remaining(),
                body=sample.model_dump_json().encode(),
                headers={"Idempotency-Key": str(prediction_id), "X-Data-Source": "demo"},
            )
        )
        if forecast.prediction_id != prediction_id:
            fail("prediction_id_mismatch")
        report.phase = "feedback"
        submission = FeedbackRequest(
            submission_id=uuid4(),
            feedback=CountFeedback(kind="actual_count", value=report.actual_count),
        )
        receipt = FeedbackReceipt.model_validate_json(
            api.request(
                f"/predictions/{prediction_id}/feedback",
                remaining(),
                body=submission.model_dump_json().encode(),
            )
        )
        report.feedback_id = str(submission.submission_id)
        if receipt.event.submission != submission or receipt.event.prediction_id != prediction_id:
            fail("feedback_mismatch")
        report.phase = "delivery"
        detail = wait_for_delivery(
            api,
            prediction_id=str(prediction_id),
            feedback_id=report.feedback_id,
            remaining=remaining,
            report=report,
        )
        if detail.event.request != sample or detail.event.data_source != "demo":
            fail("prediction_payload_mismatch")
        verify_prediction_response(forecast, detail)
        latest = detail.latest_feedback
        if latest is None or latest.event != receipt.event or latest.event.data_source != "demo":
            fail("feedback_payload_mismatch")
        report.phase = "s3_objects"
        for event in (detail.event, latest.event):
            _ = remaining()
            report.verified_keys.append(verify_object(cloud, settings, event))
        _ = remaining()
        report.status = "success"
        report.phase = "complete"
    except Exception as error:  # noqa: BLE001 -- CLI report never includes raw error/credentials.
        report.error = safe_error(error)
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    return report


class Remaining(Protocol):
    def __call__(self) -> float: ...


def wait_for_delivery(
    api: JsonAPI,
    *,
    prediction_id: str,
    feedback_id: str,
    remaining: Remaining,
    report: CheckReport,
) -> PredictionDetail:
    while True:
        detail = PredictionDetail.model_validate_json(
            api.request(f"/predictions/{prediction_id}", remaining())
        )
        latest = detail.latest_feedback
        if (
            detail.log_status == "sent"
            and latest is not None
            and str(latest.event.event_id) == feedback_id
            and latest.log_status == "sent"
        ):
            return detail
        if report.delivery_error is None:
            try:
                status = LoggingStatus.model_validate_json(api.request("/logging", remaining()))
                code = status.last_error
                if code is not None:
                    report.delivery_error = (
                        code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "DeliveryError"
                    )
            except Exception as error:  # noqa: BLE001 -- Preserve only a safe diagnostic code.
                report.delivery_error = safe_error(error)
        time.sleep(min(0.5, remaining()))


def main(
    base_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8000",
    timeout: Annotated[float, typer.Option(min=1, max=MAX_TIMEOUT)] = 60,
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    try:
        settings = LogSettings.from_env()
        report = run_check(settings, HTTPAPI(base_url), AWSCloud(settings), timeout=timeout)
    except Exception as error:  # noqa: BLE001 -- Configuration errors must also be redacted.
        report = CheckReport(error=safe_error(error))
    text = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            _ = output.write_text(text + "\n", encoding="utf-8")
        except Exception as error:  # noqa: BLE001 -- Report output failures without raw paths.
            report.status = "failed"
            report.phase = "report_output"
            report.error = safe_error(error)
            text = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    typer.echo(text)
    if report.status != "success":
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
