from dataclasses import dataclass
from typing import Final

from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingRegressor,
    RandomForestClassifier,
)

from workload.artifact import ForestArtifact, TreeArtifact
from workload.dataset import Dataset
from workload.evaluation import (
    ClassificationMetrics,
    CountMetrics,
    classification_metrics,
    count_metrics,
)
from workload.features import FEATURE_GROUPS, FEATURE_NAMES
from workload.model_types import Classifier, Regressor
from workload.schemas import FrozenModel

SEED: Final = 42


class ExperimentResult(FrozenModel):
    name: str
    feature_group: str
    classification: ClassificationMetrics


class RegressionResult(FrozenModel):
    feature_group: str
    metrics: CountMetrics


class RawTree(FrozenModel):
    children_left: list[int]
    children_right: list[int]
    feature: list[int]
    threshold: list[float]
    value: list[list[list[float]]]


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    feature_group: str
    depth: int
    leaf_size: int
    extra: bool = False
    balanced: bool = False

    def create(self) -> Classifier:
        if self.extra:
            return ExtraTreesClassifier(
                n_estimators=160,
                max_depth=self.depth,
                min_samples_leaf=self.leaf_size,
                random_state=SEED,
                n_jobs=2,
                class_weight="balanced" if self.balanced else None,
            )
        return RandomForestClassifier(
            n_estimators=160,
            max_depth=self.depth,
            min_samples_leaf=self.leaf_size,
            random_state=SEED,
            n_jobs=2,
            class_weight="balanced" if self.balanced else None,
        )


def candidates() -> tuple[Candidate, ...]:
    return tuple(
        Candidate(
            f"{group}-{kind}-{depth}-{'balanced' if balanced else 'natural'}",
            group,
            depth,
            4,
            kind == "extra",
            balanced,
        )
        for group in FEATURE_GROUPS
        for kind in ("forest", "extra")
        for depth in (8, 14)
        for balanced in (False, True)
    )


def create_regressor() -> Regressor:
    return HistGradientBoostingRegressor(
        loss="poisson",
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=15,
        l2_regularization=5,
        early_stopping=False,
        random_state=SEED,
    )


def run_validation(
    data: Dataset,
) -> tuple[Candidate, list[ExperimentResult], list[RegressionResult]]:
    train, validation = slice(0, data.train_end), slice(data.train_end, data.validation_end)
    results: list[ExperimentResult] = []
    regressions: list[RegressionResult] = []
    for group, indices in FEATURE_GROUPS.items():
        if group.endswith("no_month"):
            continue
        features = [[row[index] for index in indices] for row in data.features]
        regression = create_regressor()
        _ = regression.fit(features[train], data.counts[train])
        regressions.append(
            RegressionResult(
                feature_group=group,
                metrics=count_metrics(
                    data.counts[validation], regression.predict(features[validation]).tolist()
                ),
            )
        )
    for candidate in candidates():
        model = candidate.create()
        features = [
            [row[index] for index in FEATURE_GROUPS[candidate.feature_group]]
            for row in data.features
        ]
        _ = model.fit(features[train], data.labels[train])
        predicted = model.predict(features[validation]).tolist()
        results.append(
            ExperimentResult(
                name=candidate.name,
                feature_group=candidate.feature_group,
                classification=classification_metrics(data.labels[validation], predicted),
            )
        )
    selected = max(
        results,
        key=lambda row: (row.classification["macro_f1"], row.classification["accuracy"], row.name),
    )
    chosen = next(candidate for candidate in candidates() if candidate.name == selected.name)
    return chosen, results, regressions


def export_forest(
    model: Classifier,
    candidate: Candidate,
    version: str,
) -> ForestArtifact:
    trees: list[TreeArtifact] = []
    indices = FEATURE_GROUPS[candidate.feature_group]
    for estimator in model.estimators_:
        tree = RawTree.model_validate(estimator.tree_, from_attributes=True)
        probabilities = [row[0] for row in tree.value]
        normalized = [[value / sum(row) for value in row] for row in probabilities]
        trees.append(
            TreeArtifact(
                left=tree.children_left,
                right=tree.children_right,
                feature=[indices[index] if index >= 0 else -1 for index in tree.feature],
                threshold=tree.threshold,
                probabilities=[(float(row[0]), float(row[1]), float(row[2])) for row in normalized],
            )
        )
    return ForestArtifact(model_version=version, feature_names=FEATURE_NAMES, trees=trees)
