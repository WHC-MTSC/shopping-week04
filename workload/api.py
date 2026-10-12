from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, TypeAdapter

from workload.artifact import STAGE_RANGES, STAGES, ForestArtifact, load_artifact
from workload.features import feature_values
from workload.schemas import ForecastRequest, ForecastResponse, FrozenModel, HealthResponse

DEFAULT_MODEL_PATH: Final = Path(__file__).resolve().parents[1] / "artifacts/model.json"


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


def create_app(model_path: Path = DEFAULT_MODEL_PATH) -> FastAPI:
    model: ForestArtifact | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal model
        model = load_artifact(model_path)
        yield
        model = None

    application = FastAPI(title="Shopping workload forecast", version="0.1.0", lifespan=lifespan)

    @application.exception_handler(RequestValidationError)
    def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = TypeAdapter(list[ValidationIssue]).validate_python(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "detail": [issue.model_dump(mode="json") for issue in issues],
            },
        )

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
        loaded = ready_model()
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

    @application.post("/predict")
    def predict(payload: ForecastRequest) -> ForecastResponse:
        loaded = ready_model()
        values = feature_values(
            payload.target_hour, [observation.count for observation in payload.history]
        )
        probabilities = loaded.predict_proba(values)
        stage = max(range(3), key=lambda index: probabilities[index])
        return ForecastResponse(
            target_hour=payload.target_hour,
            workload_stage=STAGES[stage],
            stage_range=STAGE_RANGES[stage],
            probabilities=probabilities,
            model_version=loaded.model_version,
        )

    return application


app = create_app()
