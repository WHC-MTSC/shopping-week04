from datetime import datetime
from math import cos, pi, sin
from typing import Final

HISTORY_HOURS: Final = 168
BUSY_THRESHOLD: Final = 10
WEEKEND_START: Final = 5
MONTH_INDEX: Final = 2

FEATURE_NAMES: Final = (
    "hour",
    "weekday",
    "month",
    "weekend",
    "lag_1h",
    "lag_24h",
    "lag_168h",
    "previous_24h_mean",
    "previous_3h_mean",
    "previous_6h_mean",
    "previous_168h_mean",
    "previous_day_3h_mean",
    "previous_week_3h_mean",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "day_of_month",
)
FEATURE_GROUPS: Final = {
    "calendar": (0, 1, 2, 3),
    "basic": (0, 1, 2, 3, 4, 5),
    "history": tuple(range(8)),
    "extended": tuple(range(len(FEATURE_NAMES))),
    "history_no_month": (0, 1, 3, 4, 5, 6, 7),
    "extended_no_month": tuple(
        index for index in range(len(FEATURE_NAMES)) if index != MONTH_INDEX
    ),
}


def label_count(count: int) -> int:
    if count == 0:
        return 0
    return 1 if count < BUSY_THRESHOLD else 2


def feature_values(target: datetime, history: list[int]) -> list[float]:
    return [
        target.hour,
        target.weekday(),
        target.month,
        int(target.weekday() >= WEEKEND_START),
        history[-1],
        history[-24],
        history[-168],
        sum(history[-24:]) / 24,
        sum(history[-3:]) / 3,
        sum(history[-6:]) / 6,
        sum(history) / 168,
        sum(history[-24:-21]) / 3,
        sum(history[:3]) / 3,
        sin(2 * pi * target.hour / 24),
        cos(2 * pi * target.hour / 24),
        sin(2 * pi * target.weekday() / 7),
        cos(2 * pi * target.weekday() / 7),
        target.day,
    ]
