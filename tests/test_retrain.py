import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import typer

from workload import retrain as retrain_module
from workload.api import model_metadata
from workload.artifact import ForestArtifact, TreeArtifact, load_artifact
from workload.features import FEATURE_NAMES, feature_values
from workload.prepare_logs import PreparationSummary, SupervisedRow
from workload.retrain import RetrainingError, chronological_split, read_training_rows, retrain


def synthetic_rows(count: int = 60) -> list[SupervisedRow]:
    # Deliberately synthetic unit fixtures; these are never service/team logs.
    return [
        SupervisedRow(
            target_hour=datetime(2026, 1, 1) + timedelta(hours=i),
            features=feature_values(
                datetime(2026, 1, 1) + timedelta(hours=i), [(0, 5, 15)[i % 3]] * 168
            ),
            label=i % 3,
            prediction_id=UUID(int=i + 1),
            feedback_id=UUID(int=i + 1000),
            data_source="user",
            model_version="synthetic-unit-fixture",
            input_fingerprint=hashlib.sha256(str(i).encode()).hexdigest(),
        )
        for i in range(count)
    ]


def write_dataset(path: Path, rows: list[SupervisedRow], *, include_demo: bool = False) -> None:
    path.mkdir()
    _ = (path / "supervised.jsonl").write_text(
        "".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8"
    )
    summary = PreparationSummary(
        input_sha256="synthetic-unit-fixture",
        include_demo=include_demo,
        promotion_eligible=not include_demo,
        counts={"supervised_rows": len(rows)},
        quarantine_reasons={},
    )
    _ = (path / "summary.json").write_text(summary.model_dump_json(), encoding="utf-8")


@pytest.mark.parametrize(
    ("allow_demo", "include_demo"), [(False, False), (True, True), (True, False)]
)
def test_candidate_training_preserves_baseline_and_portable_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, allow_demo: bool, include_demo: bool
) -> None:
    source, output, baseline = tmp_path / "dataset", tmp_path / "candidate", tmp_path / "base.json"
    rows = synthetic_rows()
    if include_demo:
        rows = [row.model_copy(update={"data_source": "demo"}) for row in rows]
    write_dataset(source, rows, include_demo=include_demo)
    tree = TreeArtifact(
        left=[-1], right=[-1], feature=[-1], threshold=[-2], probabilities=[(1, 0, 0)]
    )
    original = ForestArtifact(model_version="baseline", feature_names=FEATURE_NAMES, trees=[tree])
    body = original.model_dump_json().encode()
    _ = baseline.write_bytes(body)
    report = retrain(source, output, baseline, allow_demo=allow_demo)
    candidate = load_artifact(output / "model.json")
    assert report.train_rows == 48
    assert report.holdout_rows == 12
    assert report.train_end < report.holdout_start
    assert report.max_probability_error <= 1e-12
    assert report.quality_gate_passed
    assert report.promotion_eligible is not allow_demo
    purpose = "historical_replay" if allow_demo else "real_feedback"
    assert report.purpose == purpose
    assert candidate.training_purpose == purpose
    assert report.candidate_version.startswith("practice-" if allow_demo else "feedback-")
    metadata = model_metadata(candidate, report.candidate_sha256)
    assert metadata.model_purpose == purpose
    assert (metadata.model_notice is not None) is allow_demo
    assert candidate.model_version == report.candidate_version
    assert (
        report.candidate_sha256 == hashlib.sha256((output / "model.json").read_bytes()).hexdigest()
    )
    assert baseline.read_bytes() == body
    for row in rows[-12:]:
        assert sum(candidate.predict_proba(row.features)) == pytest.approx(1)
    if allow_demo:

        def completed_retrain(
            _directory: Path, _output: Path, _baseline: Path, *, allow_demo: bool
        ) -> retrain_module.RetrainingReport:
            assert allow_demo
            return report

        monkeypatch.setattr(retrain_module, "retrain", completed_retrain)
        # Successful practice exits normally despite being ineligible for production promotion.
        retrain_module.main(source, output, baseline, allow_demo=True)

        def failed_gate(
            _directory: Path, _output: Path, _baseline: Path, *, allow_demo: bool
        ) -> retrain_module.RetrainingReport:
            assert allow_demo
            return report.model_copy(update={"quality_gate_passed": False})

        monkeypatch.setattr(retrain_module, "retrain", failed_gate)
        with pytest.raises(typer.Exit) as caught:
            retrain_module.main(source, output, baseline, allow_demo=True)
        assert caught.value.exit_code == 2


def test_demo_diagnostics_cannot_be_retrained(tmp_path: Path) -> None:
    write_dataset(tmp_path / "data", synthetic_rows(), include_demo=True)
    with pytest.raises(RetrainingError, match="demo_data"):
        _ = read_training_rows(tmp_path / "data")


def test_demo_row_cannot_bypass_manifest(tmp_path: Path) -> None:
    rows = synthetic_rows()
    rows[0] = rows[0].model_copy(update={"data_source": "demo"})
    write_dataset(tmp_path / "data", rows)
    with pytest.raises(RetrainingError, match="demo_data"):
        _ = read_training_rows(tmp_path / "data")


def test_legacy_artifact_without_purpose_defaults_to_historical_baseline() -> None:
    tree = TreeArtifact(
        left=[-1], right=[-1], feature=[-1], threshold=[-2], probabilities=[(1, 0, 0)]
    )
    original = ForestArtifact(model_version="legacy", feature_names=FEATURE_NAMES, trees=[tree])
    legacy = original.model_dump_json(exclude={"training_purpose"})
    loaded = ForestArtifact.model_validate_json(legacy)
    assert loaded.training_purpose == "historical_baseline"
    assert model_metadata(loaded, "legacy-hash").model_notice is None


def test_duplicate_inputs_cannot_cross_split(tmp_path: Path) -> None:
    rows = synthetic_rows()
    rows[-1] = rows[-1].model_copy(update={"input_fingerprint": rows[0].input_fingerprint})
    write_dataset(tmp_path / "data", rows)
    with pytest.raises(RetrainingError, match="duplicate_training"):
        _ = read_training_rows(tmp_path / "data")


def test_time_groups_never_cross_split() -> None:
    rows = synthetic_rows()
    rows[47] = rows[47].model_copy(update={"target_hour": rows[48].target_hour})
    train, holdout = chronological_split(rows)
    assert len(train) == 47
    assert len(holdout) == 13
    assert max(row.target_hour for row in train) < min(row.target_hour for row in holdout)


@pytest.mark.parametrize("count", [0, 3, 39])
def test_too_few_real_examples_are_rejected(count: int) -> None:
    with pytest.raises(RetrainingError, match="at_least_40"):
        _ = chronological_split(synthetic_rows(count))


def test_missing_busy_class_does_not_report_successful_validation() -> None:
    rows = [row.model_copy(update={"label": row.label % 2}) for row in synthetic_rows()]
    with pytest.raises(RetrainingError, match="all_three_stages"):
        _ = chronological_split(rows)


def test_existing_candidate_is_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "candidate"
    output.mkdir()
    marker = output / "model.json"
    _ = marker.write_text("unchanged", encoding="utf-8")
    with pytest.raises(RetrainingError, match="output_directory_must_be_new"):
        _ = retrain(tmp_path / "missing-data", output, tmp_path / "missing-base.json")
    assert marker.read_text(encoding="utf-8") == "unchanged"
