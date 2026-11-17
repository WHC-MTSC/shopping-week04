from ctypes import c_float
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from workload.features import FEATURE_NAMES
from workload.schemas import FrozenModel

STAGES: Final = ("idle", "normal", "busy")
STAGE_RANGES: Final = ("0", "1-9", "10+")
PROBABILITY_TOLERANCE: Final = 1e-8
Probability = Annotated[float, Field(ge=0, le=1)]


class TreeArtifact(FrozenModel):
    left: list[int]
    right: list[int]
    feature: list[int]
    threshold: list[float]
    probabilities: list[tuple[Probability, Probability, Probability]]

    @model_validator(mode="after")
    def check_structure(self) -> Self:
        size = len(self.left)
        if not size or any(
            len(values) != size
            for values in (
                self.right,
                self.feature,
                self.threshold,
                self.probabilities,
            )
        ):
            raise PydanticCustomError("tree_shape", "invalid tree array lengths")
        for index in range(size):
            left, right = self.left[index], self.right[index]
            leaf = left == right == -1
            if not leaf and not (index < left < size and index < right < size):
                raise PydanticCustomError("tree_edges", "tree children must point forward")
            if not leaf and not (0 <= self.feature[index] < len(FEATURE_NAMES)):
                raise PydanticCustomError("tree_feature", "invalid feature index")
            if abs(sum(self.probabilities[index]) - 1) > PROBABILITY_TOLERANCE:
                raise PydanticCustomError("tree_probability", "probabilities must sum to one")
        return self

    def predict(self, values: list[float]) -> tuple[float, float, float]:
        index = 0
        while self.left[index] != -1:
            index = (
                self.left[index]
                if values[self.feature[index]] <= self.threshold[index]
                else self.right[index]
            )
        return self.probabilities[index]


class ForestArtifact(FrozenModel):
    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    model_version: str
    feature_names: tuple[str, ...]
    stages: tuple[str, str, str] = STAGES
    trees: Annotated[list[TreeArtifact], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def check_contract(self) -> Self:
        if self.feature_names != FEATURE_NAMES or self.stages != STAGES:
            raise PydanticCustomError("model_contract", "unknown feature or label contract")
        return self

    def predict_proba(self, values: list[float]) -> tuple[float, float, float]:
        values = [c_float(value).value for value in values]
        totals = [0.0, 0.0, 0.0]
        for tree in self.trees:
            for index, value in enumerate(tree.predict(values)):
                totals[index] += value / len(self.trees)
        return totals[0], totals[1], totals[2]


def load_artifact(path: Path) -> ForestArtifact:
    return ForestArtifact.model_validate_json(path.read_text(encoding="utf-8"))
