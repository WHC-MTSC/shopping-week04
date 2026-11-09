import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from workload.export_logs import ExportError, export_logs, main, utc_milliseconds


class FakeLogs:
    def __init__(self, pages: list[dict[str, object] | Exception]) -> None:
        self.pages: list[dict[str, object] | Exception] = pages
        self.calls: list[dict[str, object]] = []

    def filter_log_events(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def make_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        _ = connection.execute("CREATE TABLE events(sequence INTEGER, body TEXT)")
        _ = connection.executemany(
            "INSERT INTO events VALUES(?, ?)",
            [(2, '{"event_id":"second","extra":42}'), (1, '{"event_id":"first"}')],
        )


def test_local_export_is_ordered_read_only_and_digest_matches(tmp_path: Path) -> None:
    database, output = tmp_path / "source.sqlite3", tmp_path / "export"
    make_database(database)
    original = database.read_bytes()
    manifest = export_logs(source="local", database=database, output=output)
    body = (output / "events.jsonl").read_bytes()
    assert body.splitlines() == [b'{"event_id": "first"}', b'{"event_id": "second", "extra": 42}']
    assert database.read_bytes() == original
    assert manifest["count"] == 2
    assert manifest["sha256"] == hashlib.sha256(body).hexdigest()
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_missing_database_does_not_create_any_source_or_export(tmp_path: Path) -> None:
    database, output = tmp_path / "missing.sqlite3", tmp_path / "export"
    with pytest.raises(ExportError, match="database_not_found"):
        _ = export_logs(source="local", database=database, output=output)
    assert not database.exists()
    assert not output.exists()
    assert not list(tmp_path.glob(".logs-export-*"))


def test_existing_export_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "export"
    output.mkdir()
    marker = output / "manifest.json"
    _ = marker.write_text("previous export")
    with pytest.raises(ExportError, match="output_already_exists"):
        _ = export_logs(source="local", output=output)
    assert marker.read_text() == "previous export"


def test_cloudwatch_empty_page_pagination_and_unparsed_messages(tmp_path: Path) -> None:
    client = FakeLogs(
        [
            {"events": [], "nextToken": "A"},
            {"events": [{"message": '{"schema_version":1,"extra":42}'}], "nextToken": "B"},
            {"events": [{"message": "server started\nready"}, {"message": "[1,2]"}]},
        ]
    )
    output = tmp_path / "export"
    result = export_logs(
        source="cloudwatch",
        output=output,
        group="existing",
        stream="team",
        start="2026-11-09T00:00:00Z",
        end="2026-11-09T01:00:00+00:00",
        profile="private-profile",
        client=client,
    )
    assert result["count"] == 3
    assert client.calls[0]["logStreamNames"] == ["team"]
    assert client.calls[1]["nextToken"] == "A"
    assert client.calls[2]["nextToken"] == "B"
    lines = (output / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {"schema_version": 1, "extra": 42}
    assert json.loads(lines[1]) == {"unparsed_message": "server started\nready"}
    assert json.loads(lines[2]) == {"unparsed_message": "[1,2]"}
    assert "private-profile" not in (output / "manifest.json").read_text()


@pytest.mark.parametrize(
    "pages",
    [
        [{"nextToken": "A"}, {"nextToken": "A"}],
        [{"nextToken": "A"}, {"nextToken": "B"}, {"nextToken": "A"}],
        [{"events": [{"message": "{}"}], "nextToken": "A"}, OSError("read failed")],
    ],
)
def test_pagination_failures_publish_no_export(
    tmp_path: Path, pages: list[dict[str, object] | Exception]
) -> None:
    output = tmp_path / "export"
    with pytest.raises((ExportError, OSError)):
        _ = export_logs(
            source="cloudwatch",
            output=output,
            group="existing",
            start="2026-11-09T00:00:00Z",
            end="2026-11-09T01:00:00Z",
            client=FakeLogs(pages),
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".logs-export-*"))


def test_record_limit_fails_instead_of_truncating(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    make_database(database)
    output = tmp_path / "export"
    with pytest.raises(ExportError, match="max_records_exceeded"):
        _ = export_logs(source="local", database=database, output=output, max_records=1)
    assert not output.exists()


@pytest.mark.parametrize(
    "value", ["2026-11-09", "2026-11-09T00:00:00+09:00", "2026-11-09T00:00:00.000001Z", "bad"]
)
def test_rejects_ambiguous_non_utc_and_submillisecond_times(value: str) -> None:
    with pytest.raises(ExportError):
        _ = utc_milliseconds(value)


def test_cli_exports_and_reports_failure_without_sdk_details(tmp_path: Path) -> None:
    app = typer.Typer()
    _ = app.command()(main)
    result = CliRunner().invoke(
        app,
        [
            "--source",
            "local",
            "--output",
            str(tmp_path / "out"),
            "--database",
            str(tmp_path / "absent"),
        ],
    )
    assert result.exit_code == 1
    assert "database_not_found" in result.output
    assert not (tmp_path / "out").exists()
