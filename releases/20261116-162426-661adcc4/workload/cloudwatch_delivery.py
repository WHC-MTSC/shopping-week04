"""At-least-once delivery to an existing CloudWatch Logs group and stream.

Retries can produce duplicate messages after an ambiguous network result. Consumers
must deduplicate by the JSON event_id; PutLogEvents does not deduplicate requests.
"""

import time
from typing import TYPE_CHECKING, Final, Protocol, cast

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from workload.log_models import Event

MAX_BATCH_BYTES: Final = 1_048_576
EVENT_OVERHEAD_BYTES: Final = 26


class CloudWatchLogsClient(Protocol):
    def put_log_events(self, **kwargs: object) -> dict[str, object]: ...


class CloudWatchSettings(Protocol):
    @property
    def profile(self) -> str | None: ...

    @property
    def region(self) -> str: ...

    @property
    def log_group(self) -> str | None: ...

    @property
    def log_stream(self) -> str | None: ...


class CloudWatchRejectedEventsError(ValueError):
    """CloudWatch returned HTTP 200 but rejected this log event."""


class CloudWatchPayloadTooLargeError(ValueError):
    """The UTF-8 message and AWS per-event overhead exceed a single batch."""


class CloudWatchSink:
    def __init__(
        self,
        settings: CloudWatchSettings,
        client: CloudWatchLogsClient | None = None,
    ) -> None:
        self.settings: CloudWatchSettings = settings
        self._client: CloudWatchLogsClient | None = client

    def send(self, event: "Event") -> None:
        group = self.settings.log_group
        stream = self.settings.log_stream
        if group is None or stream is None:
            message = "CloudWatch group and stream must already be configured"
            raise ValueError(message)
        message = event.model_dump_json()
        if len(message.encode("utf-8")) + EVENT_OVERHEAD_BYTES > MAX_BATCH_BYTES:
            raise CloudWatchPayloadTooLargeError
        if self._client is None:
            session = boto3.Session(
                profile_name=self.settings.profile,
                region_name=self.settings.region,
            )
            self._client = cast(
                "CloudWatchLogsClient",
                session.client(  # pyright: ignore[reportUnknownMemberType]
                    "logs",
                    config=Config(
                        connect_timeout=3,
                        read_timeout=5,
                        retries={"mode": "standard", "max_attempts": 2},
                    ),
                ),
            )
        # Use the current upload-attempt time, so old durable outbox events are accepted.
        # The original recorded_at and historical target_hour remain unchanged in JSON.
        result = self._client.put_log_events(
            logGroupName=group,
            logStreamName=stream,
            logEvents=[{"timestamp": time.time_ns() // 1_000_000, "message": message}],
        )
        # A single-event request makes any rejection unambiguous, including index zero.
        # Sequence tokens are intentionally omitted; AWS ignores them.
        if "rejectedLogEventsInfo" in result:
            raise CloudWatchRejectedEventsError
