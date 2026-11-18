"""Train and evaluate next-hour sales-invoice count forecasts.

The input CSV comes from shopping-week02/result/model-input.csv. Each row's
target is the count during target_hour; all model features are known at its
start, assuming the previous hour's count has finished processing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


TARGET = "positive_sales_invoice_count"
BASIC = [
    "hour_of_day",
    "day_of_week",
    "month",
    "is_weekend",
    "orders_lag_1h",
    "orders_lag_24h",
]
FULL = BASIC + ["orders_lag_168h", "orders_previous_24h_mean"]
SEED = 42


def read_data(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, parse_dates=["target_hour"])
    required = {"target_hour", TARGET, *FULL}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    if data.empty or data["target_hour"].isna().any() or data["target_hour"].duplicated().any():
        raise ValueError("target_hour must contain unique, non-null timestamps")
    if not data["target_hour"].is_monotonic_increasing:
        raise ValueError("Rows must be ordered by target_hour")
    if not data["target_hour"].diff().dropna().eq(pd.Timedelta(hours=1)).all():
        raise ValueError("Rows must represent consecutive hours")
    if data[list(required - {"target_hour"})].isna().any().any():
        raise ValueError("Target and features cannot be null")
    if (data[TARGET] < 0).any():
        raise ValueError("Target counts cannot be negative")
    return data


def feature_sets(data: pd.DataFrame) -> dict[str, pd.DataFrame]:
    engineered = data[FULL].copy()
    engineered["hour_sin"] = np.sin(2 * np.pi * data["hour_of_day"] / 24)
    engineered["hour_cos"] = np.cos(2 * np.pi * data["hour_of_day"] / 24)
    engineered["weekday_sin"] = np.sin(2 * np.pi * data["day_of_week"] / 7)
    engineered["weekday_cos"] = np.cos(2 * np.pi * data["day_of_week"] / 7)
    engineered["lag1_minus_daily"] = data["orders_lag_1h"] - data["orders_lag_24h"]
    engineered["daily_minus_weekly"] = data["orders_lag_24h"] - data["orders_lag_168h"]
    return {"basic": data[BASIC], "full": data[FULL], "engineered": engineered}


def new_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="poisson",
        max_iter=200,
        learning_rate=0.05,
        max_leaf_nodes=15,
        l2_regularization=5,
        random_state=SEED,
    )


def rounded_counts(prediction: np.ndarray) -> np.ndarray:
    return np.maximum(0, np.rint(prediction)).astype(int)


def metrics(actual: np.ndarray, prediction: np.ndarray) -> dict:
    estimate = rounded_counts(prediction)
    error = np.abs(actual - estimate)
    return {
        "rows": len(actual),
        "mae": round(float(mean_absolute_error(actual, estimate)), 4),
        "rmse": round(float(np.sqrt(mean_squared_error(actual, estimate))), 4),
        "r2": round(float(r2_score(actual, estimate)), 4),
        "exact_match_rate": round(float(np.mean(error == 0)), 4),
        "within_2_invoices_rate": round(float(np.mean(error <= 2)), 4),
        "wape": round(float(error.sum() / actual.sum()), 4) if actual.sum() else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/model-input.csv"))
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    data = read_data(args.data)
    args.out.mkdir(parents=True, exist_ok=True)
    n = len(data)
    train_end, validation_end = int(n * 0.6), int(n * 0.8)
    y = data[TARGET].to_numpy(dtype=int)
    sets = feature_sets(data)

    validation = {
        "previous_day_same_hour": metrics(
            y[train_end:validation_end],
            data["orders_lag_24h"].iloc[train_end:validation_end].to_numpy(),
        )
    }
    for name, x in sets.items():
        candidate = new_model()
        candidate.fit(x.iloc[:train_end], y[:train_end])
        validation[name] = metrics(
            y[train_end:validation_end],
            candidate.predict(x.iloc[train_end:validation_end]),
        )

    # Select using validation MAE only. The test period is never used to choose.
    selected_name = min(sets, key=lambda name: (validation[name]["mae"], name))
    selected_x = sets[selected_name]
    model = new_model()
    model.fit(selected_x.iloc[:validation_end], y[:validation_end])
    predicted = rounded_counts(model.predict(selected_x.iloc[validation_end:]))
    actual = y[validation_end:]
    baseline = data["orders_lag_24h"].iloc[validation_end:].to_numpy(dtype=int)
    high_load = actual >= 10

    test = {
        "selected_model": metrics(actual, predicted),
        "previous_day_same_hour": metrics(actual, baseline),
        "positive_hours": metrics(actual[actual > 0], predicted[actual > 0]),
        "high_load_10_or_more": metrics(actual[high_load], predicted[high_load]),
        "zero_hour_share": round(float(np.mean(actual == 0)), 4),
    }
    predictions = pd.DataFrame(
        {
            "target_hour": data["target_hour"].iloc[validation_end:].to_numpy(),
            "actual_count": actual,
            "predicted_count": predicted,
            "previous_day_same_hour": baseline,
            "absolute_error": np.abs(actual - predicted),
        }
    )
    predictions.to_csv(args.out / "test-predictions.csv", index=False)

    report = {
        "source": str(args.data).replace("\\", "/"),
        "source_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
        "target": TARGET,
        "prediction_time": "Start of target_hour; preceding completed-hour counts are available",
        "split": {
            "method": "Chronological 60% train / 20% validation / 20% untouched test",
            "train": {"rows": train_end, "start": str(data.target_hour.iloc[0]), "end": str(data.target_hour.iloc[train_end - 1])},
            "validation": {"rows": validation_end - train_end, "start": str(data.target_hour.iloc[train_end]), "end": str(data.target_hour.iloc[validation_end - 1])},
            "test": {"rows": n - validation_end, "start": str(data.target_hour.iloc[validation_end]), "end": str(data.target_hour.iloc[-1])},
        },
        "selection_metric": "validation MAE in invoices per hour",
        "validation": validation,
        "selected_model": selected_name,
        "selected_features": list(selected_x.columns),
        "test": test,
        "metric_note": "Exact match and ±2 invoice rates are count tolerances; they are not classification accuracy. WAPE is total absolute error divided by total actual invoices.",
        "library_versions": {"pandas": pd.__version__, "numpy": np.__version__, "scikit_learn": sklearn.__version__, "joblib": joblib.__version__},
    }
    (args.out / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    joblib.dump({"model": model, "features": list(selected_x.columns), "target": TARGET}, args.out / "model.joblib")
    print(json.dumps({"selected_model": selected_name, "validation": validation[selected_name], "test": test}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
