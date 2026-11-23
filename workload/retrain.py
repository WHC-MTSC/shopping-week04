"""Train a candidate on real feedback, evaluate chronologically, and retain the serving model."""

import hashlib
import json
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import sklearn
import typer

from workload.artifact import ForestArtifact
from workload.evaluation import ClassificationMetrics, classification_metrics
from workload.experiments import SEED, Candidate, export_forest
from workload.features import FEATURE_GROUPS
from workload.prepare_logs import PreparationSummary, SupervisedRow
from workload.schemas import FrozenModel

ROOT = Path(__file__).resolve().parents[1]
MIN_TRAIN = 30
MIN_HOLDOUT = 10
PARITY_TOLERANCE = 1e-12
CANDIDATE = Candidate(
    "feedback-extra-14-balanced", "extended_no_month", 14, 4, extra=True, balanced=True
)


class RetrainingError(ValueError):
    pass


class RetrainingReport(FrozenModel):
    purpose: Literal["real_feedback", "historical_replay"]
    candidate_version: str
    candidate_sha256: str
    baseline_version: str
    baseline_sha256: str
    dataset_sha256: str
    source_events_sha256: str
    sklearn_version: str
    train_rows: int
    holdout_rows: int
    train_end: datetime
    holdout_start: datetime
    baseline_metrics: ClassificationMetrics
    candidate_metrics: ClassificationMetrics
    max_probability_error: float
    quality_gate_passed: bool
    promotion_eligible: bool
    gate: str = "No decrease in chronological holdout macro F1 or busy recall."
    note: str = (
        "Minimum sample and class checks are workflow guards, not proof of production quality. "
        "The holdout is not used for fitting; no serving model is overwritten."
    )


def read_training_rows(
    directory: Path, *, allow_demo: bool = False
) -> tuple[list[SupervisedRow], PreparationSummary, bytes]:
    summary = PreparationSummary.model_validate_json((directory / "summary.json").read_bytes())
    body = (directory / "supervised.jsonl").read_bytes()
    rows = [SupervisedRow.model_validate_json(line) for line in body.splitlines() if line.strip()]
    if not allow_demo and (
        summary.include_demo
        or not summary.promotion_eligible
        or any(row.data_source != "user" for row in rows)
    ):
        message = "demo_data_is_not_eligible_for_retraining"
        raise RetrainingError(message)
    if summary.counts.get("supervised_rows") != len(rows):
        message = "dataset_manifest_row_count_mismatch"
        raise RetrainingError(message)
    if len({row.input_fingerprint for row in rows}) != len(rows):
        message = "duplicate_training_inputs"
        raise RetrainingError(message)
    return sorted(rows, key=lambda row: (row.target_hour, row.input_fingerprint)), summary, body


def chronological_split(
    rows: list[SupervisedRow],
) -> tuple[list[SupervisedRow], list[SupervisedRow]]:
    if len(rows) < MIN_TRAIN + MIN_HOLDOUT:
        message = "need_at_least_40_labelled_examples"
        raise RetrainingError(message)
    boundary = rows[min(int(len(rows) * 0.8), len(rows) - MIN_HOLDOUT)].target_hour
    train = [row for row in rows if row.target_hour < boundary]
    holdout = [row for row in rows if row.target_hour >= boundary]
    if len(train) < MIN_TRAIN or len(holdout) < MIN_HOLDOUT:
        message = "insufficient_distinct_times_for_chronological_split"
        raise RetrainingError(message)
    if {row.label for row in train} != {0, 1, 2} or {row.label for row in holdout} != {0, 1, 2}:
        message = "all_three_stages_required_in_training_and_holdout"
        raise RetrainingError(message)
    return train, holdout


def evaluate(artifact: ForestArtifact, rows: list[SupervisedRow]) -> ClassificationMetrics:
    predictions: list[int] = []
    for row in rows:
        probabilities = artifact.predict_proba(row.features)
        predictions.append(max(range(3), key=lambda index: probabilities[index]))
    return classification_metrics([row.label for row in rows], predictions)


def retrain(
    directory: Path, output: Path, baseline_path: Path, *, allow_demo: bool = False
) -> RetrainingReport:
    if output.exists():
        message = "output_directory_must_be_new"
        raise RetrainingError(message)
    rows, summary, body = read_training_rows(directory, allow_demo=allow_demo)
    train, holdout = chronological_split(rows)
    baseline_bytes = baseline_path.read_bytes()
    baseline = ForestArtifact.model_validate_json(baseline_bytes)
    indices = FEATURE_GROUPS[CANDIDATE.feature_group]
    model = CANDIDATE.create()
    _ = model.fit([[row.features[i] for i in indices] for row in train], [r.label for r in train])
    digest = hashlib.sha256(body).hexdigest()
    configuration = json.dumps(asdict(CANDIDATE), sort_keys=True)
    version = ("practice-" if allow_demo else "feedback-") + hashlib.sha256(
        f"{digest}:{configuration}:{SEED}:{sklearn.__version__}".encode()
    ).hexdigest()[:16]
    purpose = "historical_replay" if allow_demo else "real_feedback"
    artifact = export_forest(model, CANDIDATE, version).model_copy(
        update={"training_purpose": purpose}
    )
    portable = [artifact.predict_proba(row.features) for row in holdout]
    original = model.predict_proba([[row.features[i] for i in indices] for row in holdout]).tolist()
    parity = max(
        abs(left - right)
        for pp, oo in zip(portable, original, strict=True)
        for left, right in zip(pp, oo, strict=True)
    )
    if parity > PARITY_TOLERANCE:
        message = "serialized_model_parity_failed"
        raise RetrainingError(message)
    baseline_metrics = evaluate(baseline, holdout)
    candidate_metrics = evaluate(artifact, holdout)
    quality_gate_passed = (
        candidate_metrics["macro_f1"] >= baseline_metrics["macro_f1"]
        and candidate_metrics["class_recall"][2] >= baseline_metrics["class_recall"][2]
    )
    artifact_bytes = (artifact.model_dump_json() + "\n").encode()
    report = RetrainingReport(
        purpose=purpose,
        candidate_version=version,
        candidate_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        baseline_version=baseline.model_version,
        baseline_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
        dataset_sha256=digest,
        source_events_sha256=summary.input_sha256,
        sklearn_version=sklearn.__version__,
        train_rows=len(train),
        holdout_rows=len(holdout),
        train_end=train[-1].target_hour,
        holdout_start=holdout[0].target_hour,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        max_probability_error=parity,
        quality_gate_passed=quality_gate_passed,
        promotion_eligible=quality_gate_passed and not allow_demo,
        note=(
            "Historical replay practice; never promotion eligible. Holdout excluded from fitting."
            if allow_demo
            else "Chronological holdout validation only; no serving model is overwritten."
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".retrain-", dir=output.parent) as staging:
        staged = Path(staging) / "candidate"
        staged.mkdir()
        _ = (staged / "model.json").write_bytes(artifact_bytes)
        _ = (staged / "report.json").write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _ = staged.rename(output)
    return report


def main(
    dataset: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
    baseline: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = ROOT
    / "artifacts/model.json",
    *,
    allow_demo: Annotated[
        bool,
        typer.Option(
            "--allow-demo", help="Historical replay practice only; never eligible for promotion."
        ),
    ] = False,
) -> None:
    try:
        report = retrain(dataset, output, baseline, allow_demo=allow_demo)
    except (ValueError, OSError) as error:
        typer.echo(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(error)
                    if isinstance(error, RetrainingError)
                    else type(error).__name__,
                }
            ),
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(report.model_dump_json(indent=2))
    if not report.quality_gate_passed:
        raise typer.Exit(2)


if __name__ == "__main__":
    typer.run(main)
