from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from workload.features import feature_values, label_count
from workload.schemas import ForecastRequest, Observation


def test_workload_boundaries_when_invoice_counts_change() -> None:
    # Given fixed operational boundaries, When labeled, Then all edges agree.

    assert [label_count(count) for count in (0, 1, 9, 10, 100)] == [0, 1, 1, 2, 2]


def test_features_when_history_is_available_use_only_previous_hours() -> None:

    # Given completed prior hourly counts, When built, Then correct causal lags.
    values = feature_values(datetime(2011, 9, 27, 6), list(range(168)))
    assert values[4:8] == [167, 144, 0, sum(range(144, 168)) / 24]


def test_request_when_history_contains_target_hour_is_rejected() -> None:

    # Given history ending in the target, When parsed, Then prevent target leakage.
    target = datetime(2011, 9, 27, 6)
    history = [Observation(hour=target - timedelta(hours=167 - i), count=0) for i in range(168)]
    with pytest.raises(ValidationError, match="precede"):
        _ = ForecastRequest(target_hour=target, history=history)


def test_request_when_unknown_label_field_is_supplied_is_rejected() -> None:

    # Given a leaked label, When JSON parsed, Then forbid unknown inputs.
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _ = ForecastRequest.model_validate_json(
            '{"target_hour":"2011-09-27T06:00:00", "history":[], "positive_sales_invoice_count":10}'
        )
