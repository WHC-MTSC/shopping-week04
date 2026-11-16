from statistics import mean
from typing import Final, TypedDict

from workload.features import BUSY_THRESHOLD

COUNT_TOLERANCE: Final = 2


class ClassificationMetrics(TypedDict):
    rows: int
    accuracy: float
    macro_f1: float
    balanced_accuracy: float
    active_hour_accuracy: float
    class_recall: list[float]
    class_precision: list[float]
    confusion_matrix: list[list[int]]
    class_support: list[int]


class CountMetrics(TypedDict):
    mae: float
    within_2_rate: float
    exact_rate: float
    active_mae: float
    busy_mae: float


def classification_metrics(actual: list[int], prediction: list[int]) -> ClassificationMetrics:
    pairs = list(zip(actual, prediction, strict=True))
    matrix = [
        [sum(a == left and p == right for a, p in pairs) for right in range(3)] for left in range(3)
    ]
    recall = [matrix[i][i] / max(1, sum(matrix[i])) for i in range(3)]
    precision = [matrix[i][i] / max(1, sum(row[i] for row in matrix)) for i in range(3)]
    f1 = [2 * p * r / max(p + r, 1e-15) for p, r in zip(precision, recall, strict=True)]
    return ClassificationMetrics(
        rows=len(actual),
        accuracy=mean(a == p for a, p in pairs),
        macro_f1=mean(f1),
        balanced_accuracy=mean(recall),
        active_hour_accuracy=mean(a == p for a, p in pairs if a > 0),
        class_recall=recall,
        class_precision=precision,
        confusion_matrix=matrix,
        class_support=[sum(row) for row in matrix],
    )


def count_metrics(actual: list[int], prediction: list[float]) -> CountMetrics:
    pairs = list(zip(actual, prediction, strict=True))
    error = [abs(a - max(0, round(p))) for a, p in pairs]
    return CountMetrics(
        mae=mean(error),
        within_2_rate=mean(e <= COUNT_TOLERANCE for e in error),
        exact_rate=mean(e == 0 for e in error),
        active_mae=mean(e for e, a in zip(error, actual, strict=True) if a > 0),
        busy_mae=mean(e for e, a in zip(error, actual, strict=True) if a >= BUSY_THRESHOLD),
    )
