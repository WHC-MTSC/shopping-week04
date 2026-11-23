# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "numpy>=2,<3", "polars>=1.30,<2", "scikit-learn>=1.7,<2",
#   "pydantic>=2.10,<3", "typer>=0.16,<1",
# ]
# ///
# Run: uv run --locked python train.py --evaluate-test

from hashlib import sha256
from pathlib import Path
from typing import Final

import polars as pl
import sklearn
import typer

from workload.artifact import load_artifact
from workload.dataset import read_dataset
from workload.evaluation import ClassificationMetrics, classification_metrics
from workload.experiments import ExperimentResult, RegressionResult, export_forest, run_validation
from workload.features import FEATURE_GROUPS, label_count
from workload.schemas import FrozenModel

PARITY_TOLERANCE: Final = 1e-12


class ModelParityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("serialized model probability parity failed")


class TrainingReport(FrozenModel):
    service: str = (
        "Forecast the coming hour's sales-invoice workload stage from completed hourly history."
    )
    labels: tuple[str, str, str] = ("idle: 0", "normal: 1-9", "busy: 10+")
    selection_metric: str = "validation macro F1; ties: accuracy then candidate name"
    selected: str
    source_sha256: str
    source_rows: int
    warmup_rows: int = 168
    split_rows: tuple[int, int, int]
    split_boundaries: tuple[str, str, str, str]
    validation: list[ExperimentResult]
    regression_validation: list[RegressionResult]
    validation_previous_day: ClassificationMetrics
    validation_majority: ClassificationMetrics
    library_version: str
    benchmark_note: str = (
        "Historical benchmark already reported in PR #1; not a prospective unseen test."
    )
    test: ClassificationMetrics | None = None
    test_previous_day: ClassificationMetrics | None = None
    test_majority: ClassificationMetrics | None = None
    roundtrip_max_probability_error: float | None = None


def main(*, evaluate_test: bool = False) -> None:
    data_path = Path("data/model-input.csv")
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    data = read_dataset(data_path)
    chosen, validation, regression = run_validation(data)
    valid = slice(data.train_end, data.validation_end)
    baseline = [label_count(int(row[5])) for row in data.features]
    report = TrainingReport(
        selected=chosen.name,
        source_sha256=sha256(data_path.read_bytes()).hexdigest(),
        source_rows=data.original_rows,
        split_rows=(
            data.train_end,
            data.validation_end - data.train_end,
            len(data.hours) - data.validation_end,
        ),
        split_boundaries=(
            str(data.hours[0]),
            str(data.hours[data.train_end]),
            str(data.hours[data.validation_end]),
            str(data.hours[-1]),
        ),
        validation=validation,
        regression_validation=regression,
        validation_previous_day=classification_metrics(data.labels[valid], baseline[valid]),
        validation_majority=classification_metrics(
            data.labels[valid], [0] * len(data.labels[valid])
        ),
        library_version=sklearn.__version__,
    )
    _ = (output / "validation.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    print(report.model_dump_json(indent=2))
    if not evaluate_test:
        return
    model = chosen.create()
    features = [
        [row[index] for index in FEATURE_GROUPS[chosen.feature_group]] for row in data.features
    ]
    _ = model.fit(features[: data.validation_end], data.labels[: data.validation_end])
    version = sha256(
        (report.source_sha256 + chosen.name + sklearn.__version__).encode()
    ).hexdigest()[:12]
    artifact = export_forest(model, chosen, version)
    model_path = output / "model.json"
    _ = model_path.write_text(artifact.model_dump_json() + "\n", encoding="utf-8")
    loaded = load_artifact(model_path)
    test = slice(data.validation_end, len(data.hours))
    portable = [loaded.predict_proba(row) for row in data.features[test]]
    original = model.predict_proba(features[test]).tolist()
    difference = max(
        abs(p - o)
        for pp, oo in zip(portable, original, strict=True)
        for p, o in zip(pp, oo, strict=True)
    )
    if difference > PARITY_TOLERANCE:
        raise ModelParityError
    predicted = [max(range(3), key=lambda index: row[index]) for row in portable]
    final = report.model_copy(
        update={
            "test": classification_metrics(data.labels[test], predicted),
            "test_previous_day": classification_metrics(data.labels[test], baseline[test]),
            "test_majority": classification_metrics(
                data.labels[test], [0] * len(data.labels[test])
            ),
            "roundtrip_max_probability_error": difference,
        }
    )
    _ = (output / "metrics.json").write_text(
        final.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    pl.DataFrame(
        {
            "target_hour": [str(hour) for hour in data.hours[test]],
            "actual_count": data.counts[test],
            "actual_stage": data.labels[test],
            "predicted_stage": predicted,
            "previous_day_stage": baseline[test],
        }
    ).write_csv(output / "test-predictions.csv")
    print(final.model_dump_json(indent=2))


if __name__ == "__main__":
    typer.run(main)
