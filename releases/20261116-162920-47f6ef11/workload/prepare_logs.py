"""Convert validated event JSONL into supervised examples and contextual rewards."""

import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from math import isclose
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID

import typer
from pydantic import Field, NaiveDatetime, TypeAdapter, ValidationError

from workload.artifact import STAGE_RANGES, STAGES
from workload.features import FEATURE_NAMES, feature_values, label_count
from workload.log_models import (
    CorrectnessFeedback,
    CountFeedback,
    Event,
    FeedbackEvent,
    PredictionEvent,
    Stage,
)
from workload.schemas import FrozenModel

EVENT = TypeAdapter(Event)
Vector = Annotated[list[float], Field(min_length=18, max_length=18)]
Label = Annotated[int, Field(strict=True, ge=0, le=2)]


class PreparationError(ValueError):
    pass


class SupervisedRow(FrozenModel):
    target_hour: NaiveDatetime
    features: Vector
    label: Label
    prediction_id: UUID
    feedback_id: UUID
    data_source: Literal["user", "demo"]
    model_version: str
    input_fingerprint: str


class RewardRow(FrozenModel):
    target_hour: NaiveDatetime
    features: Vector
    action: Stage
    reward: Annotated[int, Field(strict=True, ge=0, le=1)]
    prediction_id: UUID
    prediction_event_id: UUID
    feedback_id: UUID
    data_source: Literal["user", "demo"]
    model_version: str
    input_fingerprint: str
    feedback_kind: Literal["correctness", "actual_count", "actual_stage"]


class QuarantineRow(FrozenModel):
    reason: str
    line: int | None = None
    event: Event | None = None


class PreparationSummary(FrozenModel):
    input_sha256: str
    include_demo: bool
    promotion_eligible: bool
    feature_names: tuple[str, ...] = FEATURE_NAMES
    counts: dict[str, int]
    quarantine_reasons: dict[str, int]
    reward_semantics: str = "Contextual action/reward feedback; no episodes or next states."
    split_policy: str = "Sort/split by target_hour; never randomly split rows."


def fingerprint(event: PredictionEvent) -> str:
    body = json.dumps(event.request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def prediction_problem(event: PredictionEvent) -> str | None:
    response = event.response
    if response.target_hour != event.request.target_hour or response.workload_stage not in STAGES:
        return "invalid_prediction_response"
    index = STAGES.index(response.workload_stage)
    if response.stage_range != STAGE_RANGES[index]:
        return "invalid_prediction_response"
    probabilities = response.probabilities
    if any(not 0 <= value <= 1 for value in probabilities) or not isclose(
        sum(probabilities), 1, rel_tol=0, abs_tol=1e-8
    ):
        return "invalid_probabilities"
    # Serving uses first-maximum argmax, including ties; rewards must describe that action.
    if index != max(range(len(probabilities)), key=lambda position: probabilities[position]):
        return "prediction_action_mismatch"
    values = feature_values(event.request.target_hour, [row.count for row in event.request.history])
    if set(event.features) != set(FEATURE_NAMES) or any(
        not isclose(event.features[name], value, rel_tol=1e-10, abs_tol=1e-10)
        for name, value in zip(FEATURE_NAMES, values, strict=True)
    ):
        return "feature_mismatch"
    return None


def feedback_problem(prediction: PredictionEvent, feedback: FeedbackEvent) -> str | None:
    if feedback.event_id != feedback.submission.submission_id:
        return "submission_id_mismatch"
    if feedback.data_source != prediction.data_source:
        return "data_source_mismatch"
    answer = feedback.submission.feedback
    if isinstance(answer, CorrectnessFeedback):
        expected_stage = None
        correct = answer.value
    else:
        expected_stage = (
            STAGES[label_count(answer.value)] if isinstance(answer, CountFeedback) else answer.value
        )
        correct = expected_stage == prediction.response.workload_stage
    if feedback.actual_stage != expected_stage or feedback.is_correct != correct:
        return "feedback_consistency"
    return None


def read_events(
    input_path: Path, quarantine: list[QuarantineRow]
) -> tuple[dict[UUID, Event], int, int, str]:
    raw = input_path.read_bytes()
    events: dict[UUID, Event] = {}
    duplicates = 0
    lines = 0
    for line, payload in enumerate(raw.splitlines(), start=1):
        if not payload.strip():
            continue
        lines += 1
        try:
            event = EVENT.validate_json(payload, strict=True)
        except ValidationError:
            quarantine.append(QuarantineRow(reason="malformed", line=line))
            continue
        previous = events.get(event.event_id)
        if previous is not None:
            if previous != event:
                message = f"conflicting_event_id:{event.event_id}"
                raise PreparationError(message)
            duplicates += 1
        else:
            events[event.event_id] = event
    return events, lines, duplicates, hashlib.sha256(raw).hexdigest()


def choose_feedback(
    prediction: PredictionEvent,
    truths: list[FeedbackEvent],
    quarantine: list[QuarantineRow],
) -> FeedbackEvent | None:
    if not truths:
        quarantine.append(QuarantineRow(reason="missing_truth", event=prediction))
        return None
    if len({row.revision for row in truths}) != len(truths):
        quarantine.extend(QuarantineRow(reason="conflicting_revision", event=row) for row in truths)
        return None
    latest = max(truths, key=lambda row: row.revision)
    problem = feedback_problem(prediction, latest)
    if problem:
        quarantine.append(QuarantineRow(reason=problem, event=latest))
        return None
    return latest


def select_pairs(
    events: dict[UUID, Event], quarantine: list[QuarantineRow]
) -> list[tuple[PredictionEvent, FeedbackEvent]]:
    predictions: dict[UUID, list[PredictionEvent]] = defaultdict(list)
    feedbacks: dict[UUID, list[FeedbackEvent]] = defaultdict(list)
    for event in events.values():
        if isinstance(event, PredictionEvent):
            predictions[event.prediction_id].append(event)
        else:
            feedbacks[event.prediction_id].append(event)
    for prediction_id, rows in feedbacks.items():
        if prediction_id not in predictions:
            quarantine.extend(QuarantineRow(reason="missing_prediction", event=row) for row in rows)
    pairs: list[tuple[PredictionEvent, FeedbackEvent]] = []
    for prediction_id, rows in predictions.items():
        if len(rows) != 1:
            quarantine.extend(
                QuarantineRow(reason="conflicting_prediction_id", event=row) for row in rows
            )
            continue
        prediction = rows[0]
        problem = prediction_problem(prediction)
        if problem:
            quarantine.append(QuarantineRow(reason=problem, event=prediction))
            continue
        latest = choose_feedback(prediction, feedbacks.get(prediction_id, []), quarantine)
        if latest is not None:
            pairs.append((prediction, latest))
    return pairs


def make_rows(
    pairs: list[tuple[PredictionEvent, FeedbackEvent]], quarantine: list[QuarantineRow]
) -> tuple[list[SupervisedRow], list[RewardRow], int]:
    groups: dict[str, list[tuple[PredictionEvent, FeedbackEvent]]] = defaultdict(list)
    for prediction, feedback in pairs:
        groups[fingerprint(prediction)].append((prediction, feedback))
    supervised: list[SupervisedRow] = []
    rewards: list[RewardRow] = []
    duplicates = 0
    for key, group in groups.items():
        labelled = [
            (prediction, feedback)
            for prediction, feedback in group
            if feedback.actual_stage is not None
        ]
        labels = {feedback.actual_stage for _, feedback in labelled}
        counts = {
            feedback.submission.feedback.value
            for _, feedback in labelled
            if isinstance(feedback.submission.feedback, CountFeedback)
        }
        # A T/F judgment cannot contradict an explicit outcome for the identical input.
        # Check consistency without inferring a supervised label from a T/F-only group.
        conflicting_reward = any(
            feedback.is_correct != (prediction.response.workload_stage == label)
            for prediction, feedback in group
            for label in labels
        )
        if len(labels) > 1 or len(counts) > 1 or conflicting_reward:
            quarantine.extend(
                QuarantineRow(reason="conflicting_input_truth", event=feedback)
                for _, feedback in group
            )
            continue
        for prediction, feedback in group:
            values = feature_values(
                prediction.request.target_hour, [row.count for row in prediction.request.history]
            )
            rewards.append(
                RewardRow(
                    target_hour=prediction.request.target_hour,
                    features=values,
                    action=cast("Stage", prediction.response.workload_stage),
                    reward=int(feedback.is_correct),
                    prediction_id=prediction.prediction_id,
                    prediction_event_id=prediction.event_id,
                    feedback_id=feedback.event_id,
                    data_source=prediction.data_source,
                    model_version=prediction.response.model_version,
                    input_fingerprint=key,
                    feedback_kind=feedback.submission.feedback.kind,
                )
            )
        if labelled:
            prediction, feedback = max(
                labelled, key=lambda pair: (pair[1].recorded_at, str(pair[1].event_id))
            )
            assert feedback.actual_stage is not None  # noqa: S101 -- Established by labelled filter.
            values = feature_values(
                prediction.request.target_hour, [row.count for row in prediction.request.history]
            )
            supervised.append(
                SupervisedRow(
                    target_hour=prediction.request.target_hour,
                    features=values,
                    label=STAGES.index(feedback.actual_stage),
                    prediction_id=prediction.prediction_id,
                    feedback_id=feedback.event_id,
                    data_source=prediction.data_source,
                    model_version=prediction.response.model_version,
                    input_fingerprint=key,
                )
            )
            duplicates += len(labelled) - 1
    supervised.sort(key=lambda row: (row.target_hour, row.input_fingerprint))
    rewards.sort(key=lambda row: (row.target_hour, str(row.feedback_id)))
    return supervised, rewards, duplicates


def write_jsonl(
    path: Path, rows: list[SupervisedRow] | list[RewardRow] | list[QuarantineRow]
) -> None:
    _ = path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")


def prepare(
    input_path: Path, output_dir: Path, *, include_demo: bool = False
) -> PreparationSummary:
    if output_dir.exists():
        message = "output_directory_exists:choose_a_new_directory"
        raise PreparationError(message)
    quarantine: list[QuarantineRow] = []
    events, lines, duplicates, digest = read_events(input_path, quarantine)
    excluded = (
        sum(event.data_source == "demo" for event in events.values()) if not include_demo else 0
    )
    selected = {
        key: event for key, event in events.items() if include_demo or event.data_source != "demo"
    }
    pairs = select_pairs(selected, quarantine)
    supervised, rewards, duplicate_inputs = make_rows(pairs, quarantine)
    summary = PreparationSummary(
        input_sha256=digest,
        include_demo=include_demo,
        promotion_eligible=not include_demo,
        counts={
            "input_events": lines,
            "unique_events": len(events),
            "duplicate_events": duplicates,
            "excluded_demo_events": excluded,
            "supervised_rows": len(supervised),
            "reward_rows": len(rewards),
            "duplicate_supervised_inputs": duplicate_inputs,
            "quarantined_records": len(quarantine),
        },
        quarantine_reasons=dict(Counter(row.reason for row in quarantine)),
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir.parent, prefix=".prepare-") as temporary:
        staging = Path(temporary)
        write_jsonl(staging / "supervised.jsonl", supervised)
        write_jsonl(staging / "rewards.jsonl", rewards)
        write_jsonl(staging / "quarantine.jsonl", quarantine)
        _ = (staging / "summary.json").write_text(
            summary.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        if output_dir.exists():
            message = "output_directory_exists:choose_a_new_directory"
            raise PreparationError(message)
        _ = staging.rename(output_dir)
    return summary


def main(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output")],
    *,
    include_demo: Annotated[bool, typer.Option("--include-demo")] = False,
) -> None:
    try:
        summary = prepare(input_path, output, include_demo=include_demo)
    except (PreparationError, OSError) as error:
        typer.echo(
            str(error) if isinstance(error, PreparationError) else type(error).__name__, err=True
        )
        raise typer.Exit(1) from None
    typer.echo(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    typer.run(main)
