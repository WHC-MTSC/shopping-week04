from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from workload.schemas import ForecastRequest, Observation


def valid_request() -> ForecastRequest:
    target = datetime(2011, 9, 27, 6)
    return ForecastRequest(
        target_hour=target,
        history=[Observation(hour=target - timedelta(hours=168 - i), count=0) for i in range(168)],
    )


@pytest.mark.parametrize("count", ["-1", "1.5", "true", "null", "NaN", "100001", '"2"'])
def test_history_when_count_is_invalid_is_rejected(count: str) -> None:
    payload = valid_request().model_dump_json().replace('"count":0', f'"count":{count}', 1)
    with pytest.raises(ValidationError):
        _ = ForecastRequest.model_validate_json(payload)


@pytest.mark.parametrize("length", [0, 167, 169])
def test_history_when_length_is_not_168_is_rejected(length: int) -> None:
    valid = valid_request()
    history = [*valid.history, valid.history[-1]][:length]
    with pytest.raises(ValidationError):
        _ = ForecastRequest(target_hour=valid.target_hour, history=history)


@pytest.mark.parametrize("offset", [-1, 1, 24])
def test_history_when_a_gap_or_duplicate_is_supplied_is_rejected(offset: int) -> None:
    valid = valid_request()
    first = valid.history[0]
    changed = Observation(hour=first.hour + timedelta(hours=offset), count=first.count)
    with pytest.raises(ValidationError, match="contiguous"):
        _ = ForecastRequest(target_hour=valid.target_hour, history=[changed, *valid.history[1:]])


@pytest.mark.parametrize("target", ["2011-09-27T06:00:00Z", "2011-09-27T06:30:00", "bad-date"])
def test_target_when_timestamp_contract_is_violated_is_rejected(target: str) -> None:
    raw = valid_request().model_dump_json().replace("2011-09-27T06:00:00", target, 1)
    with pytest.raises(ValidationError):
        _ = ForecastRequest.model_validate_json(raw)
