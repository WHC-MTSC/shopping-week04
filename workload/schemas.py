from datetime import datetime, timedelta
from typing import Annotated, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, Field, NaiveDatetime, field_validator, model_validator
from pydantic_core import PydanticCustomError

from workload.features import HISTORY_HOURS

MIN_TARGET_HOUR: Final = datetime.fromisoformat("0001-01-01T00:00:00") + timedelta(
    hours=HISTORY_HOURS
)

Count = Annotated[int, Field(strict=True, ge=0, le=100_000)]


class FrozenModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False
    )


class Observation(FrozenModel):
    hour: NaiveDatetime
    count: Count

    @field_validator("hour")
    @classmethod
    def check_hour(cls, value: datetime) -> datetime:
        if value.minute or value.second or value.microsecond:
            raise PydanticCustomError("hour_boundary", "timestamps must be at an exact hour")
        return value


class ForecastRequest(FrozenModel):
    target_hour: NaiveDatetime
    history: Annotated[list[Observation], Field(min_length=168, max_length=168)]

    @model_validator(mode="after")
    def check_history(self) -> Self:
        if self.target_hour < MIN_TARGET_HOUR:
            raise PydanticCustomError("target_range", "target_hour must allow 168 preceding hours")
        if self.target_hour.minute or self.target_hour.second or self.target_hour.microsecond:
            raise PydanticCustomError("hour_boundary", "target_hour must be at an exact hour")
        for index, observation in enumerate(self.history):
            expected = self.target_hour - timedelta(hours=168 - index)
            if observation.hour != expected:
                raise PydanticCustomError(
                    "history_order", "history must be contiguous and strictly precede target_hour"
                )
        return self


class ForecastResponse(FrozenModel):
    target_hour: datetime
    workload_stage: str
    stage_range: str
    probabilities: tuple[float, float, float]
    model_version: str


class HealthResponse(FrozenModel):
    status: str
    model_version: str
