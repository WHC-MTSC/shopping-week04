import asyncio
import logging
import os
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import TypeAdapter, model_validator

from workload.log_models import Event, LogBackend
from workload.log_store import LogStore
from workload.schemas import FrozenModel

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class LogSettings(FrozenModel):
    database: Path = Path("runtime/events.sqlite3")
    region: str = "ap-northeast-2"
    bucket: str | None = None
    prefix: str = "workload-events"
    profile: str | None = None
    backend: LogBackend = "local"
    local_s3_root: Path = Path("runtime/local-s3")
    local_s3_bucket: str = "hourahead-local-logs"

    @model_validator(mode="after")
    def resolve_backend(self) -> Self:
        if "backend" not in self.model_fields_set and self.bucket:
            object.__setattr__(self, "backend", "s3")
        if self.backend == "s3" and not self.bucket:
            message = "The s3 backend requires WORKLOAD_S3_BUCKET"
            raise ValueError(message)
        return self

    @classmethod
    def from_env(cls) -> "LogSettings":
        bucket = os.environ.get("WORKLOAD_S3_BUCKET") or None
        return cls(
            database=Path(os.environ.get("WORKLOAD_LOG_DB", "runtime/events.sqlite3")),
            region=os.environ.get("AWS_REGION", "ap-northeast-2"),
            bucket=bucket,
            prefix=os.environ.get("WORKLOAD_S3_PREFIX", "workload-events").strip("/"),
            profile=os.environ.get("AWS_PROFILE") or None,
            backend=TypeAdapter(LogBackend).validate_python(
                os.environ.get("WORKLOAD_LOG_BACKEND", "s3" if bucket else "local")
            ),
            local_s3_root=Path(os.environ.get("WORKLOAD_LOCAL_S3_ROOT", "runtime/local-s3")),
        )


class EventSink(Protocol):
    def send(self, event: Event) -> None: ...


def object_key(settings: LogSettings, event: Event) -> str:
    return (
        f"{settings.prefix}/{event.event_type}/{event.recorded_at:%Y/%m/%d}/{event.event_id}.json"
    ).lstrip("/")


class LocalS3Sink:
    """Write immutable S3-shaped JSON objects locally without AWS credentials or requests."""

    def __init__(self, settings: LogSettings) -> None:
        self.settings: LogSettings = settings

    def object_path(self, event: Event) -> Path:
        parts = [self.settings.local_s3_bucket, *object_key(self.settings, event).split("/")]
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update(f"{kind}{number}" for kind in ("COM", "LPT") for number in range(1, 10))
        # Restrict the emulated path to portable segments, including Windows device names.
        if any(
            re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", part) is None
            or part.split(".")[0].upper() in reserved
            for part in parts
        ):
            message = "Unsafe local S3 bucket or object-key segment"
            raise ValueError(message)
        root = self.settings.local_s3_root.resolve()
        target = root.joinpath(*parts).resolve()
        if not target.is_relative_to(root):
            message = "Local S3 object path escapes its configured root"
            raise ValueError(message)
        return target

    def send(self, event: Event) -> None:
        target = self.object_path(event)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = event.model_dump_json().encode("utf-8")
        # A flushed temporary file becomes visible in one create-if-absent link operation.
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".upload-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                _ = temporary.write(body)
                temporary.flush()
                os.fsync(temporary.fileno())
            except OSError:
                temporary.close()
                temporary_path.unlink(missing_ok=True)
                raise
        try:
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                if target.read_bytes() != body:
                    message = "A different event already exists at the local S3 object key"
                    raise ValueError(message) from None
        finally:
            temporary_path.unlink(missing_ok=True)


class S3Sink:
    def __init__(self, settings: LogSettings, client: "S3Client | None" = None) -> None:
        self.settings: LogSettings = settings
        self._client: S3Client | None = client

    def send(self, event: Event) -> None:
        if self._client is None:
            session = boto3.Session(
                profile_name=self.settings.profile, region_name=self.settings.region
            )
            # Only s3 stubs are installed; unrelated boto3 overloads have no types.
            self._client = session.client(  # pyright: ignore[reportUnknownMemberType]
                "s3",
                config=Config(
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"mode": "standard", "max_attempts": 2},
                ),
            )
        bucket = self.settings.bucket
        if bucket is None:
            message = "S3 bucket is not configured"
            raise ValueError(message)
        key = object_key(self.settings, event)
        body = event.model_dump_json().encode("utf-8")
        try:
            _ = self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            # A key collision is a retry success only if the immutable body is identical.
            # GetObject denial or read failure must leave this event pending.
            existing = self._client.get_object(Bucket=bucket, Key=key)
            stream = existing["Body"]
            try:
                if existing.get("ContentLength") != len(body) or stream.read(len(body) + 1) != body:
                    message = "A different event already exists at the S3 object key"
                    raise ValueError(message) from None
            finally:
                stream.close()


def build_sink(settings: LogSettings) -> EventSink | None:
    if settings.backend == "local-s3":
        return LocalS3Sink(settings)
    return S3Sink(settings) if settings.backend == "s3" else None


class LogDelivery:
    def __init__(self, store: LogStore, sink: EventSink | None) -> None:
        self.store: LogStore = store
        self.sink: EventSink | None = sink
        self._lock: threading.Lock = threading.Lock()

    def flush(self) -> int:
        if self.sink is None:
            return 0
        sent = 0
        with self._lock:
            for event in self.store.pending():
                try:
                    self.sink.send(event)
                except (BotoCoreError, ClientError, OSError, ValueError) as error:
                    # Persist only the class, never credentials, payloads or remote error text.
                    self.store.failed(event.event_id, type(error).__name__)
                    break
                self.store.acknowledge(event.event_id)
                sent += 1
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                _ = await asyncio.to_thread(self.flush)
            except sqlite3.Error:
                logging.getLogger(__name__).warning("Log database unavailable; delivery will retry")
            try:
                _ = await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
