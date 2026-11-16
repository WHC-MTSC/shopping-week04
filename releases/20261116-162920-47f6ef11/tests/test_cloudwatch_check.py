import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import cast, override
from unittest.mock import Mock

import pytest
import typer
from botocore.exceptions import ClientError

from tests.test_live_check import FakeAPI
from workload import cloudwatch_check
from workload.cloudwatch_check import CloudWatchCheckReport, run_check
from workload.log_delivery import LogSettings
from workload.log_models import FeedbackReceipt, LoggingStatus, PredictionDetail


class DemoAPI(FakeAPI):
    def __init__(self, settings: LogSettings) -> None:
        super().__init__("cloudwatch")
        self.status: LoggingStatus = LoggingStatus(
            destination="cloudwatch",
            region=settings.region,
            log_group=settings.log_group,
            log_stream=settings.log_stream,
            pending_events=0,
            sent_events=0,
            failed_events=0,
            last_error=None,
        )
        self.paths: list[str] = []

    @override
    def request(
        self,
        path: str,
        timeout: float,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        self.paths.append(path)
        if path == "/logging":
            return self.status.model_dump_json().encode()
        if path == "/example":
            return Path("examples/request.json").read_bytes()
        result = super().request(path, timeout, body=body, headers=headers)
        if path.endswith("/feedback"):
            receipt = FeedbackReceipt.model_validate_json(result)
            return (
                receipt.model_copy(update={"log_status": "cloudwatch_sent"})
                .model_dump_json()
                .encode()
            )
        if path.startswith("/predictions/"):
            detail = PredictionDetail.model_validate_json(result)
            assert detail.latest_feedback is not None
            latest = detail.latest_feedback.model_copy(update={"log_status": "cloudwatch_sent"})
            return (
                detail.model_copy(
                    update={
                        "log_status": "cloudwatch_sent",
                        "latest_feedback": latest,
                    }
                )
                .model_dump_json()
                .encode()
            )
        return result


class FakeReader:
    def __init__(self, api: DemoAPI) -> None:
        self.api: DemoAPI = api
        self.authenticated: bool = False
        self.calls: list[dict[str, object]] = []
        self.pages: deque[dict[str, object] | None] = deque()
        self.visible: bool = True
        self.duplicate: bool = False
        self.corrupt: bool = False
        self.error: Exception | None = None

    def authenticate(self, timeout: float) -> None:
        assert timeout > 0
        self.authenticated = True

    def filter_page(self, request: dict[str, object], timeout: float) -> dict[str, object]:
        assert self.authenticated
        assert timeout > 0
        self.calls.append(request.copy())
        if self.error is not None:
            raise self.error
        if self.pages:
            page = self.pages.popleft()
            if page is not None:
                return page
        if not self.visible:
            return {"events": []}
        events = self.api.events.copy()
        if self.duplicate:
            events.append(events[0])
        if self.corrupt:
            events[0] = events[0].model_copy(update={"data_source": "user"})
        return {
            "events": [
                {"message": event.model_dump_json(), "logStreamName": self.api.status.log_stream}
                for event in events
            ]
        }


@pytest.fixture
def settings() -> LogSettings:
    return LogSettings(backend="cloudwatch", log_group="/existing/group", log_stream="existing")


def test_success_requires_two_remote_bodies_and_accepts_identical_duplicates(
    settings: LogSettings,
) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.duplicate = True
    report = run_check(settings, api, reader)
    assert report.status == "success"
    assert report.authentication == "verified"
    assert report.purpose == "diagnostic_demo"
    assert report.actual_count == 0
    assert "/example" in api.paths
    assert report.verified_event_ids == sorted(str(event.event_id) for event in api.events)
    request = reader.calls[0]
    assert request["logGroupName"] == settings.log_group
    assert request["logStreamNames"] == [settings.log_stream]
    for event in api.events:
        assert str(event.event_id) in cast("str", request["filterPattern"])


def test_empty_page_with_next_token_continues(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.pages.extend([{"events": [], "nextToken": "next"}, None])
    report = run_check(settings, api, reader)
    assert report.status == "success"
    assert reader.calls[1]["nextToken"] == "next"


def test_empty_completed_scan_retries_until_visibility(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.pages.extend([{"events": []}, None])
    assert run_check(settings, api, reader).status == "success"
    assert len(reader.calls) == 2
    assert "nextToken" not in reader.calls[1]


def test_repeated_page_token_fails_instead_of_looping(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    page: dict[str, object] = {"events": [], "nextToken": "cycle"}
    reader.pages.extend([page, page])
    report = run_check(settings, api, reader)
    assert report.status == "failed"
    assert report.error == "invalid_or_repeated_cloudwatch_token"
    assert len(reader.calls) == 2


def test_sent_status_without_remote_events_cannot_succeed(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.visible = False
    report = run_check(settings, api, reader, timeout=1)
    assert report.status == "failed"
    assert report.phase == "cloudwatch_read"
    assert report.error == "timeout"
    assert report.verified_event_ids == []


def test_matching_id_with_different_body_fails(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.corrupt = True
    report = run_check(settings, api, reader)
    assert report.status == "failed"
    assert report.error == "cloudwatch_payload_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination", "s3"),
        ("region", "us-east-1"),
        ("log_group", "/wrong/group"),
        ("log_stream", "wrong-stream"),
    ],
)
def test_wrong_destination_stops_before_authentication_and_writes(
    settings: LogSettings,
    field: str,
    value: str,
) -> None:
    api = DemoAPI(settings)
    api.status = api.status.model_copy(update={field: value})
    reader = FakeReader(api)
    report = run_check(settings, api, reader)
    assert report.status == "failed"
    assert report.phase == "preflight"
    assert not reader.authenticated
    assert not api.events


def test_missing_historical_truth_never_submits_guessed_feedback(
    settings: LogSettings,
    tmp_path: Path,
) -> None:
    truth = tmp_path / "truth.csv"
    _ = truth.write_text("target_hour,actual_count\n", encoding="utf-8")
    api = DemoAPI(settings)
    reader = FakeReader(api)
    report = run_check(settings, api, reader, truth=truth)
    assert report.error == "historical_truth_missing_or_ambiguous"
    assert not reader.authenticated
    assert not api.events


def test_aws_error_report_is_redacted(settings: LogSettings) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    reader.error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "SECRET"}}, "FilterLogEvents"
    )
    report = run_check(settings, api, reader)
    assert report.status == "failed"
    assert report.phase == "cloudwatch_read"
    assert report.error == "AccessDeniedException"
    assert "SECRET" not in repr(report)


def test_report_write_failure_exits_nonzero_without_success_output(
    settings: LogSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(LogSettings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(
        cloudwatch_check,
        "run_bounded_check",
        Mock(return_value=CloudWatchCheckReport(status="success", phase="complete")),
    )
    with pytest.raises(typer.Exit) as caught:
        cloudwatch_check.main(output=tmp_path)
    assert caught.value.exit_code == 1
    output = capsys.readouterr().out
    report = cast("dict[str, object]", json.loads(output))
    assert report["status"] == "failed"
    assert report["phase"] == "report_output"
    assert '"status": "success"' not in output
    assert str(tmp_path) not in output


def test_last_remote_response_past_deadline_cannot_report_success(
    settings: LogSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = DemoAPI(settings)
    reader = FakeReader(api)
    clock = [0.0]
    original = reader.filter_page

    def delayed_page(request: dict[str, object], timeout: float) -> dict[str, object]:
        page = original(request, timeout)
        clock[0] = 2.0
        return page

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(reader, "filter_page", delayed_page)
    report = run_check(settings, api, reader, timeout=1)
    assert report.status == "failed"
    assert report.phase == "cloudwatch_read"
    assert report.error == "timeout"


def test_cli_worker_timeout_bounds_blocked_credential_process_without_aws(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "orphan.txt"
    helper_code = (
        f"import pathlib,time;time.sleep(2);pathlib.Path({str(marker)!r}).write_text('orphan')"
    )
    blocked_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{helper_code!r}]);"
        "print('SECRET',flush=True);time.sleep(30)"
    )

    def synthetic_worker(_base_url: str, _timeout: float) -> list[str]:
        return [sys.executable, "-c", blocked_code]

    monkeypatch.setattr(cloudwatch_check, "worker_command", synthetic_worker)
    started = time.monotonic()
    report = cloudwatch_check.run_bounded_check("http://unused.invalid", 1)
    assert time.monotonic() - started < 8
    assert report.status == "failed"
    assert report.phase == "worker_timeout"
    assert report.error == "timeout"
    assert "SECRET" not in repr(report)
    time.sleep(1.5)
    assert not marker.exists(), "The worker's credential helper must also be terminated"
