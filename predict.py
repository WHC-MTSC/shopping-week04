"""Predict invoice counts for rows with the saved model's input features."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path("artifacts/model.joblib"))
    args = parser.parse_args()
    saved = joblib.load(args.model)
    data = pd.read_csv(args.input)
    features = saved["features"]
    missing = set(features).difference(data.columns)
    if missing:
        raise ValueError(f"Missing features: {sorted(missing)}")
    if data[features].isna().any().any():
        raise ValueError("Features cannot be null")
    predicted = np.maximum(0, np.rint(saved["model"].predict(data[features]))).astype(int)
    result = pd.DataFrame({"predicted_count": predicted})
    if "target_hour" in data:
        result.insert(0, "target_hour", data["target_hour"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print(f"Wrote {len(result)} predictions to {args.output}")


if __name__ == "__main__":
    main()
