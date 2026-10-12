from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from workload.artifact import ForestArtifact, TreeArtifact, load_artifact
from workload.experiments import Candidate, export_forest
from workload.features import FEATURE_NAMES, feature_values


def test_json_roundtrip_when_real_forest_is_exported(tmp_path: Path) -> None:
    candidate = Candidate("test", "history", 8, 1)
    model = candidate.create()
    full = [feature_values(datetime(2011, 9, 27, i), [i] * 168) for i in range(24)]
    selected = [row[:8] for row in full]
    _ = model.fit(selected, [i % 3 for i in range(24)])
    artifact = export_forest(model, candidate, "test")
    path = tmp_path / "model.json"
    _ = path.write_text(artifact.model_dump_json(), encoding="utf-8")
    loaded = load_artifact(path)
    for values, probabilities in zip(full, model.predict_proba(selected).tolist(), strict=True):
        assert loaded.predict_proba(values) == pytest.approx(probabilities, abs=1e-12)


def test_tree_when_cycle_is_supplied_is_rejected() -> None:
    with pytest.raises(ValidationError, match="forward"):
        _ = TreeArtifact(left=[0], right=[0], feature=[0], threshold=[0], probabilities=[(1, 0, 0)])


def test_model_when_unknown_schema_is_supplied_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ = ForestArtifact.model_validate_json('{"schema_version":2}')


def test_json_inference_when_threshold_is_near_float32_boundary_matches_sklearn() -> None:

    tree = TreeArtifact(
        left=[1, -1, -1],
        right=[2, -1, -1],
        feature=[0, -1, -1],
        threshold=[0.1, -2, -2],
        probabilities=[(0.5, 0.5, 0), (1, 0, 0), (0, 1, 0)],
    )
    model = ForestArtifact(model_version="test", feature_names=FEATURE_NAMES, trees=[tree])
    assert model.predict_proba([0.1] * len(FEATURE_NAMES)) == (0, 1, 0)
