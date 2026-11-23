import sqlite3
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from workload.artifact import STAGES
from workload.features import label_count
from workload.log_models import (
    CorrectnessFeedback,
    CountFeedback,
    EventType,
    FeedbackEvent,
    FeedbackReceipt,
    FeedbackRequest,
    LoggingStatus,
    LogPage,
    PredictionDetail,
    Stage,
)
from workload.log_store import IdempotencyConflictError, LogStore, PredictionMissingError


def register_log_errors(application: FastAPI) -> None:
    @application.exception_handler(IdempotencyConflictError)
    def conflict_handler(_request: Request, _exc: IdempotencyConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "Request ID has different content"})

    @application.exception_handler(PredictionMissingError)
    def missing_handler(_request: Request, _exc: PredictionMissingError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Prediction not found"})

    @application.exception_handler(sqlite3.Error)
    def storage_error_handler(_request: Request, _exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Local log storage unavailable; retry with the same request ID"},
        )


def log_router(store: LogStore, region: str) -> APIRouter:
    router = APIRouter()

    @router.get("/predictions/{prediction_id}", summary="저장된 예측과 최신 정답 조회")
    def prediction_detail(prediction_id: UUID) -> PredictionDetail:
        return store.get_prediction(prediction_id)

    @router.post("/predictions/{prediction_id}/feedback", summary="사용자 정답 기록")
    def feedback(prediction_id: UUID, payload: FeedbackRequest) -> FeedbackReceipt:
        prediction = store.get_prediction(prediction_id).event
        actual_stage: Stage | None = None
        answer = payload.feedback
        if isinstance(answer, CorrectnessFeedback):
            is_correct = answer.value
        else:
            actual_stage = (
                STAGES[label_count(answer.value)]
                if isinstance(answer, CountFeedback)
                else answer.value
            )
            is_correct = actual_stage == prediction.response.workload_stage
        return store.save_feedback(
            FeedbackEvent(
                event_id=payload.submission_id,
                prediction_id=prediction_id,
                recorded_at=datetime.now(UTC),
                data_source=prediction.data_source,
                submission=payload,
                actual_stage=actual_stage,
                is_correct=is_correct,
            )
        )

    @router.get("/logging", summary="로그 저장 및 S3 전송 상태")
    def logging_status() -> LoggingStatus:
        return store.status(region)

    @router.get("/logs", summary="수집한 예측·정답 로그 검색")
    def logs(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: Annotated[int | None, Query(ge=1)] = None,
        event_type: EventType | None = None,
        prediction_id: UUID | None = None,
        query: Annotated[str | None, Query(max_length=128)] = None,
    ) -> LogPage:
        return store.list_events(
            limit=limit,
            before=before,
            event_type=event_type,
            prediction_id=prediction_id,
            query=query,
        )

    return router
