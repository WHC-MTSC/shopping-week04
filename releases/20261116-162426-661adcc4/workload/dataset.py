import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from math import isclose
from pathlib import Path
from typing import Final

from pydantic_core import PydanticCustomError

from workload.features import HISTORY_HOURS, feature_values, label_count
from workload.schemas import Observation

MINIMUM_ROWS: Final = 1000


@dataclass(frozen=True, slots=True)
class Dataset:
    hours: tuple[datetime, ...]
    counts: list[int]
    features: list[list[float]]
    labels: list[int]
    train_end: int
    validation_end: int
    original_rows: int


def read_dataset(path: Path) -> Dataset:
    with path.open(encoding="utf-8-sig", newline="") as source:
        raw_rows = list(csv.DictReader(source))
    rows = tuple(
        Observation(
            hour=datetime.fromisoformat(row["target_hour"]),
            count=int(row["positive_sales_invoice_count"]),
        )
        for row in raw_rows
    )
    if len(rows) < MINIMUM_ROWS:
        raise PydanticCustomError("dataset_size", "at least 1000 hourly rows required")
    for left, right in pairwise(rows):
        if right.hour - left.hour != timedelta(hours=1):
            raise PydanticCustomError(
                "dataset_order", "source must contain consecutive unique hours"
            )
    counts = [row.count for row in rows]
    features = [
        feature_values(row.hour, counts[index - 168 : index])
        for index, row in enumerate(rows)
        if index >= HISTORY_HOURS
    ]
    stored_names = (
        "hour_of_day",
        "day_of_week",
        "month",
        "is_weekend",
        "orders_lag_1h",
        "orders_lag_24h",
        "orders_lag_168h",
        "orders_previous_24h_mean",
    )
    if not all(
        isclose(value, float(raw[name]), rel_tol=1e-12, abs_tol=1e-12)
        for raw, values in zip(raw_rows[168:], features, strict=True)
        for name, value in zip(stored_names, values[:8], strict=True)
    ):
        raise PydanticCustomError(
            "source_features", "source calendar/lags disagree with causal history"
        )
    return Dataset(
        hours=tuple(row.hour for row in rows[168:]),
        counts=counts[168:],
        features=features,
        labels=[label_count(count) for count in counts[168:]],
        train_end=int(len(rows) * 0.6) - 168,
        validation_end=int(len(rows) * 0.8) - 168,
        original_rows=len(rows),
    )
