"""Diagnose demo HTTP predictions and historical truth against actual CloudWatch Logs."""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Protocol, cast
from uuid import uuid4

import boto3
import typer
from botocore.config import Config
from pydantic import TypeAdapter

from workload.live_check import (
    HTTPAPI,
    MAX_TIMEOUT,
    TRUTH,
    JsonAPI,
    fail,
    historical_truth,
    safe_error,
    verify_prediction_response,
)
from workload.log_delivery import LogSettings
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


class CloudWatchReader(Protocol):
    def authenticate(self, timeout: float) -> None: ...

    def filter_page(self, request: dict[str, object], timeout: float) -> dict[str, object]: ...


class IdentityClient(Protocol):
    def get_caller_identity(self) -> object: ...

    def close(self) -> None: ...


class LogsClient(Protocol):
    def filter_log_events(self, **kwargs: object) -> dict[str, object]: ...

    def close(self) -> None: ...


def request_config(timeout: float) -> Config:
    allowance = min(timeout / 2, 5)
    return Config(connect_timeout=allowance, read_timeout=allowance, retries={"max_attempts": 0})


class AWSCloudWatchReader:
    def __init__(self, settings: LogSettings) -> None:
        self.settings: LogSettings = settings
        self.session: boto3.Session | None = None

    def authenticate(self, timeout: float) -> None:
        session = boto3.Session(
            profile_name=self.settings.profile,
            region_name=self.settings.region,
        )
        client = cast(
            "IdentityClient",
            session.client(  # pyright: ignore[reportUnknownMemberType]
                "sts",
                config=request_config(timeout),
            ),
        )
        try:
            _ = client.get_caller_identity()
        finally:
            client.close()
        self.session = session

    def filter_page(self, request: dict[str, object], timeout: float) -> dict[str, object]:
        if self.session is None:
            fail("aws_not_authenticated")
        client = cast(
            "LogsClient",
            self.session.client(  # pyright: ignore[reportUnknownMemberType]
                "logs",
                config=request_config(timeout),
            ),
        )
        try:
            return client.filter_log_events(**request)
        finally:
            client.close()


@dataclass
class CloudWatchCheckReport:
    status: str = "failed"
    phase: str = "preflight"
    error: str | None = None
    delivery_error: str | None = None
    purpose: str = "diagnostic_demo"
    data_source: str = "demo"
    authentication: str = "not_checked"
    region: str = ""
    log_group: str | None = None
    log_stream: str | None = None
    prediction_id: str | None = None
    feedback_id: str | None = None
    actual_count: int | None = None
    verified_event_ids: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0


class Deadline:
    def __init__(self, timeout: float) -> None:
        if not 1 <= timeout <= MAX_TIMEOUT:
            fail("timeout_must_be_1_to_300_seconds")
        self.started: float = time.monotonic()
        self.timeout: float = timeout

    def remaining(self) -> float:
        seconds = self.remaining_budget()
        if seconds <= 0:
            fail("timeout")
        return min(seconds, 5)

    def remaining_budget(self) -> float:
        return max(0, self.timeout - (time.monotonic() - self.started))

    def pause(self) -> None:
        time.sleep(min(0.5, self.remaining()))


def require_destination(settings: LogSettings, status: LoggingStatus) -> None:
    if settings.backend != "cloudwatch" or status.destination != "cloudwatch":
        fail("actual_cloudwatch_backend_required")
    if status.region != settings.region:
        fail("region_mismatch")
    if status.log_group != settings.log_group or status.log_stream != settings.log_stream:
        fail("cloudwatch_destination_mismatch")


def wait_for_delivery(
    api: JsonAPI,
    forecast: RecordedForecast,
    receipt: FeedbackReceipt,
    deadline: Deadline,
    report: CloudWatchCheckReport,
) -> PredictionDetail:
    while True:
        detail = PredictionDetail.model_validate_json(
            api.request(
                f"/predictions/{forecast.prediction_id}",
                deadline.remaining(),
            )
        )
        verify_prediction_response(forecast, detail)
        latest = detail.latest_feedback
        if latest is not None and latest.event != receipt.event:
            fail("feedback_payload_mismatch")
        if (
            detail.log_status == "cloudwatch_sent"
            and latest is not None
            and latest.log_status == "cloudwatch_sent"
        ):
            return detail
        if report.delivery_error is None:
            try:
                status = LoggingStatus.model_validate_json(
                    api.request("/logging", deadline.remaining())
                )
                code = status.last_error
                if code is not None:
                    report.delivery_error = (
                        code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "DeliveryError"
                    )
            except Exception as error:  # noqa: BLE001 -- Retain safe diagnostic codes only.
                report.delivery_error = safe_error(error)
        deadline.pause()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def compare_page(
    page: dict[str, object],
    expected: dict[str, str],
    stream: str,
) -> set[str]:
    events = page.get("events", [])
    if not isinstance(events, list):
        fail("invalid_cloudwatch_events")
    found: set[str] = set()
    for item in cast("list[object]", events):
        if not isinstance(item, dict):
            fail("invalid_cloudwatch_event")
        event = cast("dict[str, object]", item)
        if event.get("logStreamName") != stream:
            fail("cloudwatch_stream_mismatch")
        message = event.get("message")
        if not isinstance(message, str):
            fail("invalid_cloudwatch_message")
        body = cast("object", json.loads(message))
        if not isinstance(body, dict):
            fail("invalid_cloudwatch_message")
        record = cast("dict[str, object]", body)
        identifier = record.get("event_id")
        if isinstance(identifier, str) and identifier in expected:
            if canonical(record) != expected[identifier]:
                fail("cloudwatch_payload_mismatch")
            found.add(identifier)
    return found


def verify_remote(
    cloud: CloudWatchReader,
    settings: LogSettings,
    events: tuple[Event, Event],
    deadline: Deadline,
) -> list[str]:
    group, stream = settings.log_group, settings.log_stream
    if group is None or stream is None:
        fail("cloudwatch_destination_missing")
    expected = {
        str(event.event_id): canonical(cast("object", json.loads(event.model_dump_json())))
        for event in events
    }
    if len(expected) != len(events):
        fail("event_id_collision")
    conditions = " || ".join(f'$.event_id = "{identifier}"' for identifier in expected)
    first_timestamp = min(int(event.recorded_at.timestamp() * 1000) for event in events)
    base: dict[str, object] = {
        "logGroupName": group,
        "logStreamNames": [stream],
        "startTime": max(0, first_timestamp - 120_000),
        "filterPattern": "{ " + conditions + " }",
        "limit": 100,
    }
    found: set[str] = set()
    while True:
        request = base.copy()
        seen_tokens: set[str] = set()
        while True:
            page = cloud.filter_page(request, deadline.remaining())
            _ = deadline.remaining()
            found.update(compare_page(page, expected, stream))
            token = page.get("nextToken")
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                fail("invalid_or_repeated_cloudwatch_token")
            seen_tokens.add(token)
            request = {**base, "nextToken": token}
        if found == set(expected):
            _ = deadline.remaining()
            return sorted(found)
        deadline.pause()


def run_check(
    settings: LogSettings,
    api: JsonAPI,
    cloud: CloudWatchReader,
    *,
    timeout: float = 60,
    truth: Path = TRUTH,
) -> CloudWatchCheckReport:
    report = CloudWatchCheckReport(
        region=settings.region,
        log_group=settings.log_group,
        log_stream=settings.log_stream,
    )
    started = time.monotonic()
    try:
        deadline = Deadline(timeout)
        status = LoggingStatus.model_validate_json(api.request("/logging", deadline.remaining()))
        require_destination(settings, status)
        report.phase = "historical_truth"
        sample = ForecastRequest.model_validate_json(api.request("/example", deadline.remaining()))
        report.actual_count = historical_truth(truth, sample)
        report.phase = "authentication"
        cloud.authenticate(deadline.remaining())
        report.authentication = "verified"
        report.phase = "prediction"
        report.prediction_id = str(uuid4())
        forecast = RecordedForecast.model_validate_json(
            api.request(
                "/predict",
                deadline.remaining(),
                body=sample.model_dump_json().encode(),
                headers={"Idempotency-Key": report.prediction_id, "X-Data-Source": "demo"},
            )
        )
        if str(forecast.prediction_id) != report.prediction_id:
            fail("prediction_id_mismatch")
        report.phase = "feedback"
        submission = FeedbackRequest(
            submission_id=uuid4(),
            feedback=CountFeedback(
                kind="actual_count",
                value=report.actual_count,
            ),
        )
        report.feedback_id = str(submission.submission_id)
        receipt = FeedbackReceipt.model_validate_json(
            api.request(
                f"/predictions/{report.prediction_id}/feedback",
                deadline.remaining(),
                body=submission.model_dump_json().encode(),
            )
        )
        if (
            receipt.event.submission != submission
            or receipt.event.data_source != "demo"
            or receipt.event.prediction_id != forecast.prediction_id
            or receipt.event.event_id != submission.submission_id
        ):
            fail("feedback_payload_mismatch")
        report.phase = "delivery"
        detail = wait_for_delivery(api, forecast, receipt, deadline, report)
        if detail.event.request != sample or detail.event.data_source != "demo":
            fail("prediction_payload_mismatch")
        report.phase = "cloudwatch_read"
        report.verified_event_ids = verify_remote(
            cloud, settings, (detail.event, receipt.event), deadline
        )
        _ = deadline.remaining()
        report.status, report.phase = "success", "complete"
    except Exception as error:  # noqa: BLE001 -- Never report credentials or event contents.
        report.error = safe_error(error)
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    return report


def stop_worker(process: subprocess.Popen[bytes]) -> None:
    """Stop only the worker tree created by this invocation, including credential helpers."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _ = subprocess.run(  # noqa: S603 -- Fixed tool targeting only our own child PID.
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S607
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    _ = process.wait(timeout=5)


def worker_command(base_url: str, timeout: float) -> list[str]:
    return [
        sys.executable,
        "-m",
        "workload.cloudwatch_check",
        "--worker",
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
    ]


def run_bounded_check(base_url: str, timeout: float) -> CloudWatchCheckReport:
    """Bound startup, credential resolution/refresh, requests and worker exit together."""
    deadline = Deadline(timeout)
    # Regular files cannot hang on inherited pipes left open by a credential_process.
    with (
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
        subprocess.Popen(  # noqa: S603 -- Fixed Python module, argument list, no shell.
            worker_command(base_url, timeout),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process,
    ):
        try:
            _ = process.wait(timeout=deadline.remaining_budget())
        except subprocess.TimeoutExpired:
            stop_worker(process)
            return CloudWatchCheckReport(
                phase="worker_timeout",
                error="timeout",
                elapsed_seconds=round(time.monotonic() - deadline.started, 3),
            )
        _ = stdout.seek(0)
        report = TypeAdapter(CloudWatchCheckReport).validate_json(stdout.read(65_536))
        if deadline.remaining_budget() == 0:
            report.status, report.phase, report.error = "failed", "worker_timeout", "timeout"
        if process.returncode != 0 and report.status == "success":
            report.status, report.phase, report.error = "failed", "worker", "worker_failed"
        return report


def main(
    base_url: Annotated[str, typer.Option()] = "http://127.0.0.1:8000",
    timeout: Annotated[float, typer.Option(min=1, max=MAX_TIMEOUT)] = 60,
    output: Annotated[Path | None, typer.Option()] = None,
    *,
    worker: Annotated[bool, typer.Option("--worker", hidden=True)] = False,
) -> None:
    try:
        if worker:
            settings = LogSettings.from_env()
            report = run_check(
                settings, HTTPAPI(base_url), AWSCloudWatchReader(settings), timeout=timeout
            )
        else:
            report = run_bounded_check(base_url, timeout)
    except Exception as error:  # noqa: BLE001 -- Configuration errors are also redacted.
        report = CloudWatchCheckReport(error=safe_error(error))
    text = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            _ = output.write_text(text + "\n", encoding="utf-8")
        except Exception as error:  # noqa: BLE001 -- Report file errors without raw paths.
            report.status, report.phase = "failed", "report_output"
            report.error = safe_error(error)
            text = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    typer.echo(text)
    if report.status != "success":
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
