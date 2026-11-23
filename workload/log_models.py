from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool

from workload.schemas import Count, ForecastRequest, ForecastResponse, FrozenModel

Stage = Literal["idle", "normal", "busy"]
Delivery = Literal["local", "pending", "sent", "simulated"]
LogBackend = Literal["local", "s3", "local-s3"]
EventType = Literal["prediction", "feedback"]


class CorrectnessFeedback(FrozenModel):
    kind: Literal["correctness"]
    value: StrictBool


class CountFeedback(FrozenModel):
    kind: Literal["actual_count"]
    value: Count


class StageFeedback(FrozenModel):
    kind: Literal["actual_stage"]
    value: Stage


FeedbackValue = Annotated[
    CorrectnessFeedback | CountFeedback | StageFeedback, Field(discriminator="kind")
]


class FeedbackRequest(FrozenModel):
    submission_id: UUID
    feedback: FeedbackValue


class PredictionEvent(FrozenModel):
    schema_version: Literal[1] = 1
    event_type: Literal["prediction"] = "prediction"
    event_id: UUID
    prediction_id: UUID
    recorded_at: AwareDatetime
    data_source: Literal["user", "demo"]
    request: ForecastRequest
    response: ForecastResponse
    features: dict[str, float]
    model_sha256: str


class FeedbackEvent(FrozenModel):
    schema_version: Literal[1] = 1
    event_type: Literal["feedback"] = "feedback"
    event_id: UUID
    prediction_id: UUID
    recorded_at: AwareDatetime
    revision: Annotated[int, Field(strict=True, ge=1)] = 1
    data_source: Literal["user", "demo"] = "user"
    submission: FeedbackRequest
    actual_stage: Stage | None
    is_correct: bool


Event = Annotated[PredictionEvent | FeedbackEvent, Field(discriminator="event_type")]


class RecordedForecast(ForecastResponse):
    prediction_id: UUID
    recorded_at: AwareDatetime
    log_status: Delivery


class FeedbackReceipt(FrozenModel):
    event: FeedbackEvent
    log_status: Delivery


class PredictionDetail(FrozenModel):
    event: PredictionEvent
    log_status: Delivery
    latest_feedback: FeedbackReceipt | None


class LoggingStatus(FrozenModel):
    destination: LogBackend
    region: str
    pending_events: int
    sent_events: int
    failed_events: int
    last_error: str | None


class LogListItem(FrozenModel):
    sequence: int
    event: Event
    log_status: Delivery


class LogPage(FrozenModel):
    items: list[LogListItem]
    next_cursor: int | None
    total: int
