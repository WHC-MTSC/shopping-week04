import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import TypeAdapter

from workload.log_models import (
    Delivery,
    Event,
    EventType,
    FeedbackEvent,
    FeedbackReceipt,
    LoggingStatus,
    LogListItem,
    LogPage,
    PredictionDetail,
    PredictionEvent,
)


class IdempotencyConflictError(Exception):
    """An existing request key was reused for different content."""


class PredictionMissingError(Exception):
    """Feedback must refer to an existing prediction."""


class LogStore:
    def __init__(self, path: Path, *, remote: bool = False, simulated: bool = False) -> None:
        if remote and simulated:
            message = "A log store cannot target real and simulated S3 simultaneously"
            raise ValueError(message)
        self.path: Path = path
        self.remote: bool = remote
        self.simulated: bool = simulated
        # Existing sent=1 rows mean real S3; bit 2 records local emulation separately.
        self._delivery_mask: int = 2 if simulated else 1 if remote else 3

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            _ = connection.execute("PRAGMA journal_mode=WAL")
            _ = connection.execute("PRAGMA synchronous=FULL")
            _ = connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    prediction_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS events_prediction ON events(prediction_id, sequence);
                CREATE INDEX IF NOT EXISTS events_pending ON events(sent, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS one_prediction
                    ON events(prediction_id) WHERE event_type = 'prediction';
            """)

    def delivery(self, sent: int) -> Delivery:
        if self.simulated:
            return "simulated" if sent & 2 else "pending"
        if self.remote:
            return "sent" if sent & 1 else "pending"
        if sent & 1:
            return "sent"
        if sent & 2:
            return "simulated"
        return "local"

    @staticmethod
    def insert(connection: sqlite3.Connection, event: Event) -> None:
        _ = connection.execute(
            "INSERT INTO events(event_id, prediction_id, event_type, body) VALUES (?, ?, ?, ?)",
            (
                str(event.event_id),
                str(event.prediction_id),
                event.event_type,
                event.model_dump_json(),
            ),
        )

    def save_prediction(self, event: PredictionEvent) -> PredictionDetail:
        with self.connection() as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            row = cast(
                "tuple[str, int] | None",
                connection.execute(
                    """SELECT body, sent FROM events
                       WHERE prediction_id=? AND event_type='prediction'""",
                    (str(event.prediction_id),),
                ).fetchone(),
            )
            if row is not None:
                existing = PredictionEvent.model_validate_json(row[0])
                if existing.request != event.request or existing.data_source != event.data_source:
                    raise IdempotencyConflictError
            else:
                self.insert(connection, event)
        return self.get_prediction(event.prediction_id)

    def get_prediction(self, prediction_id: UUID) -> PredictionDetail:
        with self.connection() as connection:
            row = cast(
                "tuple[str, int] | None",
                connection.execute(
                    """SELECT body, sent FROM events
                       WHERE prediction_id=? AND event_type='prediction'""",
                    (str(prediction_id),),
                ).fetchone(),
            )
            if row is None:
                raise PredictionMissingError
            feedback = cast(
                "tuple[str, int] | None",
                connection.execute(
                    """SELECT body, sent FROM events WHERE prediction_id=? AND event_type='feedback'
                       ORDER BY sequence DESC LIMIT 1""",
                    (str(prediction_id),),
                ).fetchone(),
            )
        return PredictionDetail(
            event=PredictionEvent.model_validate_json(row[0]),
            log_status=self.delivery(row[1]),
            latest_feedback=(
                FeedbackReceipt(
                    event=FeedbackEvent.model_validate_json(feedback[0]),
                    log_status=self.delivery(feedback[1]),
                )
                if feedback is not None
                else None
            ),
        )

    def save_feedback(self, event: FeedbackEvent) -> FeedbackReceipt:
        with self.connection() as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            prediction = cast(
                "tuple[int] | None",
                connection.execute(
                    """SELECT 1 FROM events
                       WHERE prediction_id=? AND event_type='prediction'""",
                    (str(event.prediction_id),),
                ).fetchone(),
            )
            if prediction is None:
                raise PredictionMissingError
            row = cast(
                "tuple[str, int] | None",
                connection.execute(
                    "SELECT body, sent FROM events WHERE event_id=?", (str(event.event_id),)
                ).fetchone(),
            )
            if row is not None:
                existing = TypeAdapter(Event).validate_json(row[0])
                if (
                    not isinstance(existing, FeedbackEvent)
                    or existing.prediction_id != event.prediction_id
                    or existing.submission != event.submission
                ):
                    raise IdempotencyConflictError
                return FeedbackReceipt(event=existing, log_status=self.delivery(row[1]))
            revision = cast(
                "tuple[int]",
                connection.execute(
                    """SELECT count(*) + 1 FROM events
                       WHERE prediction_id=? AND event_type='feedback'""",
                    (str(event.prediction_id),),
                ).fetchone(),
            )[0]
            # Commit order, rather than wall-clock time or UUID order, defines the latest answer.
            recorded = event.model_copy(update={"revision": revision})
            self.insert(connection, recorded)
        return FeedbackReceipt(event=recorded, log_status=self.delivery(0))

    def pending(self, limit: int = 20) -> list[Event]:
        with self.connection() as connection:
            rows = cast(
                "list[tuple[str]]",
                connection.execute(
                    "SELECT body FROM events WHERE (sent & ?)=0 ORDER BY sequence LIMIT ?",
                    (self._delivery_mask, limit),
                ).fetchall(),
            )
        return [TypeAdapter(Event).validate_json(row[0]) for row in rows]

    def acknowledge(self, event_id: UUID) -> None:
        with self.connection() as connection:
            _ = connection.execute(
                """UPDATE events SET sent=(sent | ?), attempts=attempts+1, last_error=NULL
                   WHERE event_id=?""",
                (2 if self.simulated else 1, str(event_id)),
            )

    def failed(self, event_id: UUID, code: str) -> None:
        with self.connection() as connection:
            _ = connection.execute(
                "UPDATE events SET attempts=attempts+1, last_error=? WHERE event_id=?",
                (code, str(event_id)),
            )

    def status(self, region: str) -> LoggingStatus:
        with self.connection() as connection:
            row = cast(
                "tuple[int, int, int]",
                connection.execute(
                    """SELECT coalesce(sum((sent & ?)=0),0), coalesce(sum((sent & ?)>0),0),
                       coalesce(sum((sent & ?)=0 AND last_error IS NOT NULL),0) FROM events""",
                    (self._delivery_mask,) * 3,
                ).fetchone(),
            )
            error = cast(
                "tuple[str] | None",
                connection.execute(
                    """SELECT last_error FROM events WHERE (sent & ?)=0 AND last_error IS NOT NULL
                       ORDER BY sequence DESC LIMIT 1""",
                    (self._delivery_mask,),
                ).fetchone(),
            )
        return LoggingStatus(
            destination="local-s3" if self.simulated else "s3" if self.remote else "local",
            region=region,
            pending_events=row[0],
            sent_events=row[1],
            failed_events=row[2],
            last_error=error[0] if error else None,
        )

    def list_events(
        self,
        *,
        limit: int = 50,
        before: int | None = None,
        event_type: EventType | None = None,
        prediction_id: UUID | None = None,
        query: str | None = None,
    ) -> LogPage:
        identifier = str(prediction_id) if prediction_id else None
        filters = (event_type, event_type, identifier, identifier, query, query)
        with self.connection() as connection:
            # Count and page share one read snapshot; new inserts cannot disturb this page.
            _ = connection.execute("BEGIN")
            total = cast(
                "tuple[int]",
                connection.execute(
                    """SELECT count(*) FROM events
                       WHERE (? IS NULL OR event_type=?)
                       AND (? IS NULL OR prediction_id=?)
                       AND (? IS NULL OR instr(body, ?)>0)""",
                    filters,
                ).fetchone(),
            )[0]
            rows = cast(
                "list[tuple[int, str, int]]",
                connection.execute(
                    """SELECT sequence, body, sent FROM events
                       WHERE (? IS NULL OR event_type=?)
                       AND (? IS NULL OR prediction_id=?)
                       AND (? IS NULL OR instr(body, ?)>0)
                       AND (? IS NULL OR sequence<?)
                       ORDER BY sequence DESC LIMIT ?""",
                    (*filters, before, before, limit + 1),
                ).fetchall(),
            )
        items = [
            LogListItem(
                sequence=row[0],
                event=TypeAdapter(Event).validate_json(row[1]),
                log_status=self.delivery(row[2]),
            )
            for row in rows[:limit]
        ]
        return LogPage(
            items=items,
            next_cursor=items[-1].sequence if len(rows) > limit else None,
            total=total,
        )
