"""Export local or CloudWatch event messages without changing the source."""

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Generator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, NoReturn, Protocol, cast

import boto3
import typer
from botocore.config import Config

Source = Literal["local", "cloudwatch"]
DEFAULT_DATABASE = Path("runtime/events.sqlite3")


class ExportError(ValueError):
    pass


def fail(code: str) -> NoReturn:
    raise ExportError(code)


class LogsClient(Protocol):
    def filter_log_events(self, **kwargs: object) -> dict[str, object]: ...


def utc_milliseconds(value: str) -> int:
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        fail("invalid_utc_time")
    if instant.utcoffset() != timedelta(0) or instant.microsecond % 1000:
        fail("utc_time_requires_offset_and_millisecond_precision")
    elapsed = instant - datetime(1970, 1, 1, tzinfo=UTC)
    milliseconds = elapsed.days * 86_400_000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
    if milliseconds < 0:
        fail("time_precedes_epoch")
    return milliseconds


def local_messages(database: Path) -> Generator[str]:
    if not database.is_file():
        fail("database_not_found")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        _ = connection.execute("PRAGMA query_only=ON")
        cursor = connection.execute("SELECT body FROM events ORDER BY sequence")
        for row in cast("Iterable[tuple[object, ...]]", cursor):
            body = row[0]
            if not isinstance(body, str):
                fail("invalid_database_body")
            yield body
    finally:
        connection.close()


def cloudwatch_messages(
    client: LogsClient, *, group: str, stream: str | None, start: int, end: int
) -> Generator[str]:
    request: dict[str, object] = {"logGroupName": group, "startTime": start, "endTime": end}
    if stream is not None:
        request["logStreamNames"] = [stream]
    seen_tokens: set[str] = set()
    while True:
        page = client.filter_log_events(**request)
        events = page.get("events", [])
        if not isinstance(events, list):
            fail("invalid_cloudwatch_events")
        for event in cast("list[object]", events):
            if not isinstance(event, dict):
                fail("invalid_cloudwatch_event")
            message = cast("dict[str, object]", event).get("message")
            if not isinstance(message, str):
                fail("invalid_cloudwatch_message")
            yield message
        token = page.get("nextToken")
        if token is None:
            return
        if not isinstance(token, str) or not token or token in seen_tokens:
            fail("invalid_or_repeated_cloudwatch_token")
        seen_tokens.add(token)
        request["nextToken"] = token


def reject_constant(_value: str) -> NoReturn:
    fail("non_json_constant")


def normalize_message(message: str) -> dict[str, object]:
    try:
        value = cast("object", json.loads(message, parse_constant=reject_constant))
    except ValueError:
        return {"unparsed_message": message}
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return {"unparsed_message": message}


def require_new_output(output: Path) -> None:
    if output.exists() or output.is_symlink():
        fail("output_already_exists")


def export_logs(  # noqa: PLR0913 -- Explicit source options match the CLI contract.
    *,
    source: Source,
    output: Path,
    database: Path = DEFAULT_DATABASE,
    group: str | None = None,
    stream: str | None = None,
    start: str | None = None,
    end: str | None = None,
    profile: str | None = None,
    region: str = "ap-northeast-2",
    max_records: int = 100_000,
    client: LogsClient | None = None,
) -> dict[str, object]:
    if source not in {"local", "cloudwatch"} or max_records < 1:
        fail("invalid_export_options")
    require_new_output(output)
    metadata: dict[str, object] = {"source": source}
    if source == "local":
        if any(value is not None for value in (group, stream, start, end)):
            fail("cloudwatch_options_require_cloudwatch_source")
        messages = local_messages(database)
    else:
        if not group or not start or not end or stream == "":
            fail("cloudwatch_requires_group_start_end")
        start_ms, end_ms = utc_milliseconds(start), utc_milliseconds(end)
        if start_ms >= end_ms:
            fail("start_must_precede_end")
        if client is None:
            session = boto3.Session(profile_name=profile, region_name=region)
            client = cast(
                "LogsClient",
                session.client(  # pyright: ignore[reportUnknownMemberType]
                    "logs",
                    config=Config(
                        connect_timeout=5,
                        read_timeout=30,
                        retries={"mode": "standard", "max_attempts": 3},
                    ),
                ),
            )
        messages = cloudwatch_messages(
            client, group=group, stream=stream, start=start_ms, end=end_ms
        )
        metadata.update(
            group=group,
            stream=stream,
            region=region,
            start_utc=start,
            end_utc=end,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".logs-export-", dir=output.parent) as temporary:
        staging = Path(temporary)
        digest = hashlib.sha256()
        count = 0
        try:
            with (staging / "events.jsonl").open("wb") as destination:
                for message in messages:
                    count += 1
                    if count > max_records:
                        fail("max_records_exceeded")
                    line = (
                        json.dumps(normalize_message(message), ensure_ascii=False, allow_nan=False)
                        + "\n"
                    ).encode("utf-8")
                    _ = destination.write(line)
                    digest.update(line)
        finally:
            messages.close()
        metadata.update(
            status="complete",
            count=count,
            sha256=digest.hexdigest(),
            file="events.jsonl",
            exported_at=datetime.now(UTC).isoformat(),
        )
        _ = (staging / "manifest.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        require_new_output(output)
        _ = staging.rename(output)
    return metadata


def main(  # noqa: PLR0913 -- Public command-line options.
    *,
    source: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()],
    database: Annotated[Path, typer.Option()] = DEFAULT_DATABASE,
    group: Annotated[str | None, typer.Option()] = None,
    stream: Annotated[str | None, typer.Option()] = None,
    start: Annotated[str | None, typer.Option()] = None,
    end: Annotated[str | None, typer.Option()] = None,
    profile: Annotated[str | None, typer.Option()] = None,
    region: Annotated[str, typer.Option()] = "ap-northeast-2",
    max_records: Annotated[int, typer.Option(min=1)] = 100_000,
) -> None:
    if source not in {"local", "cloudwatch"}:
        message = "source must be local or cloudwatch"
        raise typer.BadParameter(message)
    try:
        result = export_logs(
            source=cast("Source", source),
            output=output,
            database=database,
            group=group,
            stream=stream,
            start=start,
            end=end,
            profile=profile,
            region=region,
            max_records=max_records,
        )
    except Exception as error:  # noqa: BLE001 -- Do not print SDK credentials or event bodies.
        code = str(error) if isinstance(error, ExportError) else type(error).__name__
        typer.echo(json.dumps({"status": "failed", "error": code}), err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    typer.run(main)
