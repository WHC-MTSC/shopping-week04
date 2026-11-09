import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_log_delivery import example_event
from workload.artifact import STAGE_RANGES, STAGES
from workload.features import FEATURE_NAMES, feature_values, label_count
from workload.log_models import (
    CorrectnessFeedback,
    CountFeedback,
    Event,
    FeedbackEvent,
    FeedbackRequest,
    PredictionEvent,
    StageFeedback,
)
from workload.prepare_logs import PreparationError, RewardRow, SupervisedRow, prepare


def prediction() -> PredictionEvent:
    event = example_event()
    values = feature_values(event.request.target_hour, [row.count for row in event.request.history])
    return event.model_copy(
        update={"data_source": "user", "features": dict(zip(FEATURE_NAMES, values, strict=True))}
    )


def truth(event: PredictionEvent, value: int | bool, revision: int = 1) -> FeedbackEvent:  # noqa: FBT001 -- The feedback input is deliberately typed.
    answer = (
        CorrectnessFeedback(kind="correctness", value=value)
        if isinstance(value, bool)
        else CountFeedback(kind="actual_count", value=value)
    )
    stage = None if isinstance(value, bool) else STAGES[label_count(value)]
    request = FeedbackRequest(submission_id=uuid4(), feedback=answer)
    return FeedbackEvent(
        event_id=request.submission_id,
        prediction_id=event.prediction_id,
        recorded_at=event.recorded_at,
        revision=revision,
        data_source=event.data_source,
        submission=request,
        actual_stage=stage,
        is_correct=value if isinstance(value, bool) else stage == event.response.workload_stage,
    )


def write_events(path: Path, events: list[Event]) -> None:
    _ = path.write_text(
        "".join(event.model_dump_json() + "\n" for event in events), encoding="utf-8"
    )


def test_latest_revision_duplicate_event_and_digest(tmp_path: Path) -> None:
    event = prediction()
    old = truth(event, 0)
    latest = truth(event, 12, 2)
    source = tmp_path / "input.jsonl"
    write_events(source, [latest, event, old, event])
    summary = prepare(source, tmp_path / "out")
    row = SupervisedRow.model_validate_json((tmp_path / "out/supervised.jsonl").read_text())
    assert row.label == 2
    assert row.feedback_id == latest.event_id
    assert len(row.features) == 18
    assert summary.counts["duplicate_events"] == 1
    assert summary.input_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("correct", [True, False])
def test_correctness_is_reward_only(tmp_path: Path, *, correct: bool) -> None:
    event = prediction()
    source = tmp_path / "input.jsonl"
    write_events(source, [event, truth(event, correct)])
    summary = prepare(source, tmp_path / "out")
    assert summary.counts["supervised_rows"] == 0
    row = RewardRow.model_validate_json((tmp_path / "out/rewards.jsonl").read_text())
    assert row.reward == int(correct)
    assert row.action == event.response.workload_stage


def test_demo_default_excluded_and_diagnostics_never_promotable(tmp_path: Path) -> None:
    event = prediction().model_copy(update={"data_source": "demo"})
    source = tmp_path / "input.jsonl"
    write_events(source, [event, truth(event, 0)])
    regular = prepare(source, tmp_path / "out")
    assert regular.counts["excluded_demo_events"] == 2
    assert regular.counts["supervised_rows"] == 0
    diagnostic = prepare(source, tmp_path / "diagnostic", include_demo=True)
    assert diagnostic.counts["supervised_rows"] == 1
    assert diagnostic.promotion_eligible is False


def test_conflicting_event_id_fails_before_output(tmp_path: Path) -> None:
    event = prediction()
    source = tmp_path / "input.jsonl"
    write_events(source, [event, event.model_copy(update={"model_sha256": "different"})])
    with pytest.raises(PreparationError, match="conflicting_event_id"):
        _ = prepare(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_same_revision_rejected(tmp_path: Path) -> None:
    event = prediction()
    source = tmp_path / "input.jsonl"
    write_events(source, [event, truth(event, 0), truth(event, 10)])
    result = prepare(source, tmp_path / "out")
    assert result.counts["supervised_rows"] == 0
    assert result.quarantine_reasons == {"conflicting_revision": 2}


@pytest.mark.parametrize("second_count", [2, 3, 10])
def test_same_input_deduplicates_or_quarantines_conflicting_truth(
    tmp_path: Path, second_count: int
) -> None:
    first, second = prediction(), prediction()
    source = tmp_path / "input.jsonl"
    write_events(source, [first, truth(first, 2), second, truth(second, second_count)])
    result = prepare(source, tmp_path / "out")
    if second_count == 2:
        assert result.counts["supervised_rows"] == 1
        assert result.counts["duplicate_supervised_inputs"] == 1
    else:
        assert result.counts["supervised_rows"] == 0
        assert result.quarantine_reasons["conflicting_input_truth"] == 2


@pytest.mark.parametrize("problem", ["features", "probabilities", "feedback"])
def test_semantic_corruption_quarantined(tmp_path: Path, problem: str) -> None:
    event = prediction()
    feedback = truth(event, 0)
    if problem == "features":
        event = event.model_copy(update={"features": {**event.features, "lag_1h": 999}})
    elif problem == "probabilities":
        event = event.model_copy(
            update={
                "response": event.response.model_copy(update={"probabilities": (0.2, 0.2, 0.2)})
            }
        )
    else:
        feedback = feedback.model_copy(update={"is_correct": not feedback.is_correct})
    source = tmp_path / "input.jsonl"
    write_events(source, [event, feedback])
    result = prepare(source, tmp_path / "out")
    assert result.counts["supervised_rows"] == 0
    assert result.counts["quarantined_records"] == 1


def test_missing_and_malformed_quarantine_without_raw_secret(tmp_path: Path) -> None:
    missing_truth, missing_prediction = prediction(), prediction()
    source = tmp_path / "input.jsonl"
    write_events(source, [missing_truth, truth(missing_prediction, 0)])
    with source.open("a", encoding="utf-8") as stream:
        _ = stream.write('{"unexpected_secret":"not-copied"}\n')
    result = prepare(source, tmp_path / "out")
    assert result.quarantine_reasons == {
        "malformed": 1,
        "missing_prediction": 1,
        "missing_truth": 1,
    }
    assert "not-copied" not in (tmp_path / "out/quarantine.jsonl").read_text()


def test_actual_stage_is_supervised_label(tmp_path: Path) -> None:
    event = prediction()
    feedback = truth(event, 12)
    feedback = feedback.model_copy(
        update={
            "submission": feedback.submission.model_copy(
                update={"feedback": StageFeedback(kind="actual_stage", value="busy")}
            )
        }
    )
    source = tmp_path / "input.jsonl"
    write_events(source, [event, feedback])
    _ = prepare(source, tmp_path / "out")
    row = SupervisedRow.model_validate_json((tmp_path / "out/supervised.jsonl").read_text())
    assert row.label == 2


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "supervised.jsonl"
    _ = sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(PreparationError, match="output_directory_exists"):
        _ = prepare(tmp_path / "unused.jsonl", output)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("probabilities", [(0.0, 0.0, 1.0), (0.5, 0.5, 0.0)])
def test_action_inconsistent_with_probabilities_is_quarantined(
    tmp_path: Path, probabilities: tuple[float, float, float]
) -> None:
    event = prediction()
    event = event.model_copy(
        update={
            "response": event.response.model_copy(
                update={
                    "workload_stage": "normal",
                    "stage_range": STAGE_RANGES[1],
                    "probabilities": probabilities,
                }
            )
        }
    )
    source = tmp_path / "input.jsonl"
    write_events(source, [event, truth(event, value=True)])
    result = prepare(source, tmp_path / "out")
    assert result.counts["reward_rows"] == 0
    assert result.quarantine_reasons == {"prediction_action_mismatch": 1}


def test_correctness_conflicting_with_same_input_actual_truth_is_quarantined(
    tmp_path: Path,
) -> None:
    first, second = prediction(), prediction()
    actual_count = 12
    expected_correctness = second.response.workload_stage == "busy"
    source = tmp_path / "input.jsonl"
    write_events(
        source, [first, truth(first, actual_count), second, truth(second, not expected_correctness)]
    )
    result = prepare(source, tmp_path / "out")
    assert result.counts["supervised_rows"] == 0
    assert result.counts["reward_rows"] == 0
    assert result.quarantine_reasons == {"conflicting_input_truth": 2}
