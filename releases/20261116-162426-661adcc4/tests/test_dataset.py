import csv
from pathlib import Path

import pytest
from pydantic_core import PydanticCustomError

from workload.artifact import load_artifact
from workload.dataset import read_dataset


def test_source_when_bom_and_stored_lags_are_present_matches_contract() -> None:
    data = read_dataset(Path("data/model-input.csv"))
    assert len(data.hours) == 8619
    assert (data.train_end, data.validation_end) == (5104, 6861)
    assert str(data.hours[data.validation_end]) == "2011-09-27 06:00:00"


def test_source_when_an_hour_is_removed_is_rejected(tmp_path: Path) -> None:
    with Path("data/model-input.csv").open(encoding="utf-8-sig", newline="") as source:
        records = list(csv.reader(source))
    path = tmp_path / "gap.csv"
    with path.open("w", encoding="utf-8", newline="") as destination:
        csv.writer(destination).writerows(records[:400] + records[401:])
    with pytest.raises(PydanticCustomError, match="consecutive"):
        _ = read_dataset(path)


def test_committed_model_when_served_matches_every_benchmark_prediction() -> None:
    data = read_dataset(Path("data/model-input.csv"))
    model = load_artifact(Path("artifacts/model.json"))
    with Path("artifacts/test-predictions.csv").open(encoding="utf-8", newline="") as source:
        recorded = list(csv.DictReader(source))
    for values, row in zip(data.features[data.validation_end :], recorded, strict=True):
        probabilities = model.predict_proba(values)
        predicted = max(range(3), key=lambda index: probabilities[index])
        assert predicted == int(row["predicted_stage"])
