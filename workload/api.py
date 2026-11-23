import asyncio
import hashlib
import mimetypes
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, TypeAdapter

from workload.artifact import STAGE_RANGES, STAGES, ForestArtifact
from workload.features import FEATURE_NAMES, feature_values
from workload.log_delivery import EventSink, LogDelivery, LogSettings, build_sink
from workload.log_models import PredictionEvent, RecordedForecast
from workload.log_routes import log_router, register_log_errors
from workload.log_store import LogStore
from workload.schemas import ForecastRequest, ForecastResponse, FrozenModel, HealthResponse

DEFAULT_MODEL_PATH: Final = Path(__file__).resolve().parents[1] / "artifacts/model.json"
STATIC_PATH: Final = Path(__file__).resolve().parent / "static"
EXAMPLE_PATH: Final = Path(__file__).resolve().parents[1] / "examples/request.json"


class MetadataResponse(FrozenModel):
    model_version: str
    feature_names: tuple[str, ...]
    used_feature_names: tuple[str, ...]
    feature_note: str = (
        "feature_names is the shared serving vector; used_feature_names are forest split features."
    )
    stages: tuple[str, str, str]
    ranges: tuple[str, str, str]
    prediction_time: str = "Start of target_hour; the previous hour must be completely aggregated."
    timestamp_convention: str = "Naive UK retail local timestamps matching the original dataset."
    history_hours: int = 168
    audience: str = "쇼핑몰 운영 담당자: 정각에 다음 한 시간의 업무 준비를 판단합니다."
    output_note: str = (
        "판매전표 건수를 대리 지표로 삼은 작업량 단계이며, 정확한 건수나 필요 인원은 아닙니다."
    )
    logging_enabled: bool = True
    probability_note: str = "Uncalibrated ensemble probabilities; not guaranteed confidence levels."
    limitation: str = (
        "Historical 2010-2011 UK retail prototype; "
        "validate on new store data before staffing decisions."
    )


class ValidationIssue(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    type: str
    loc: tuple[str | int, ...]
    msg: str


def model_metadata(loaded: ForestArtifact) -> MetadataResponse:
    used_indices = {index for tree in loaded.trees for index in tree.feature if index >= 0}
    return MetadataResponse(
        model_version=loaded.model_version,
        feature_names=loaded.feature_names,
        used_feature_names=tuple(
            name for index, name in enumerate(loaded.feature_names) if index in used_indices
        ),
        stages=loaded.stages,
        ranges=STAGE_RANGES,
    )


def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    issues = TypeAdapter(list[ValidationIssue]).validate_python(exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": [issue.model_dump(mode="json") for issue in issues]},
    )


def create_app(
    model_path: Path = DEFAULT_MODEL_PATH,
    *,
    log_settings: LogSettings | None = None,
    log_sink: EventSink | None = None,
) -> FastAPI:
    model: ForestArtifact | None = None
    model_hash = ""
    settings = log_settings or LogSettings.from_env()
    store = LogStore(
        settings.database,
        remote=settings.backend == "s3",
        simulated=settings.backend == "local-s3",
    )
    delivery = LogDelivery(store, log_sink or build_sink(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal model, model_hash
        model_bytes = await asyncio.to_thread(model_path.read_bytes)
        model = ForestArtifact.model_validate_json(model_bytes)
        model_hash = hashlib.sha256(model_bytes).hexdigest()
        store.initialize()
        stop = asyncio.Event()
        worker = asyncio.create_task(delivery.run(stop))
        try:
            yield
        finally:
            stop.set()
            await worker
            model = None

    application = FastAPI(
        title="쇼핑몰 시간당 작업량 예측",
        description=(
            "쇼핑몰 운영 담당자가 완료된 직전 168시간의 판매전표 건수로 "
            "목표 정각부터 한 시간의 작업량 단계(없음 0건 / 보통 1~9건 / 혼잡 10건 이상)를 "
            "확인하는 로컬 검증용 서비스입니다. 원본 영국 현지 시각을 사용합니다. "
            "입력·예측·사용자 정답을 로컬에 기록하고, 설정된 S3로 재시도하며 전송합니다."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    # Windows registry mappings can otherwise serve ES modules as text/plain.
    mimetypes.add_type("text/javascript", ".mjs")
    application.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

    @application.get("/", response_class=FileResponse, include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_PATH / "index.html", headers={"Cache-Control": "no-cache"})

    @application.get("/log-console", response_class=FileResponse, include_in_schema=False)
    def log_console() -> FileResponse:
        return FileResponse(STATIC_PATH / "logs.html", headers={"Cache-Control": "no-cache"})

    @application.get("/example", summary="화면 체험용 과거 입력 데이터")
    def example() -> ForecastRequest:
        return ForecastRequest.model_validate_json(EXAMPLE_PATH.read_text(encoding="utf-8"))

    _ = application.exception_handler(RequestValidationError)(validation_error_handler)
    register_log_errors(application)
    application.include_router(log_router(store, settings.region))

    def ready_model() -> ForestArtifact:
        if model is None:
            raise HTTPException(status_code=503, detail="Model not ready")
        return model

    @application.get("/health")
    def health() -> HealthResponse:
        loaded = ready_model()
        return HealthResponse(status="ready", model_version=loaded.model_version)

    @application.get("/metadata")
    def metadata() -> MetadataResponse:
        return model_metadata(ready_model())

    @application.post("/predict", summary="다음 한 시간의 작업량 단계 예측")
    def predict(
        payload: ForecastRequest,
        idempotency_key: Annotated[UUID | None, Header()] = None,
        x_data_source: Annotated[Literal["user", "demo"], Header()] = "user",
    ) -> RecordedForecast:
        loaded = ready_model()
        values = feature_values(
            payload.target_hour, [observation.count for observation in payload.history]
        )
        probabilities = loaded.predict_proba(values)
        stage = max(range(3), key=lambda index: probabilities[index])
        response = ForecastResponse(
            target_hour=payload.target_hour,
            workload_stage=STAGES[stage],
            stage_range=STAGE_RANGES[stage],
            probabilities=probabilities,
            model_version=loaded.model_version,
        )
        record = store.save_prediction(
            PredictionEvent(
                event_id=uuid4(),
                prediction_id=idempotency_key or uuid4(),
                recorded_at=datetime.now(UTC),
                data_source=x_data_source,
                request=payload,
                response=response,
                features=dict(zip(FEATURE_NAMES, values, strict=True)),
                model_sha256=model_hash,
            )
        )
        return RecordedForecast(
            target_hour=record.event.response.target_hour,
            workload_stage=record.event.response.workload_stage,
            stage_range=record.event.response.stage_range,
            probabilities=record.event.response.probabilities,
            model_version=record.event.response.model_version,
            prediction_id=record.event.prediction_id,
            recorded_at=record.event.recorded_at,
            log_status=record.log_status,
        )

    return application


app = create_app()
