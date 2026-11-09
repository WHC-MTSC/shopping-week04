from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.test_log_delivery import example_event
from workload.log_store import CLOUDWATCH_DESTINATION_KEY, CloudWatchBindingError, LogStore


def test_first_binding_and_same_destination_restart_preserve_acknowledgements(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.db"
    store = LogStore(path, cloudwatch=True)
    store.initialize()
    store.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", "team-a")
    event = example_event()
    _ = store.save_prediction(event)
    store.acknowledge(event.event_id)
    restarted = LogStore(path, cloudwatch=True)
    restarted.initialize()
    restarted.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", "team-a")
    assert restarted.get_prediction(event.prediction_id).log_status == "cloudwatch_sent"
    assert restarted.pending() == []


@pytest.mark.parametrize(
    "destination",
    [
        ("eu-west-1", "/team/logs", "team-a"),
        ("ap-northeast-2", "/other/logs", "team-a"),
        ("ap-northeast-2", "/team/logs", "team-b"),
    ],
)
def test_changed_destination_refused_even_before_first_send(
    tmp_path: Path, destination: tuple[str, str, str]
) -> None:
    path = tmp_path / "events.db"
    store = LogStore(path, cloudwatch=True)
    store.initialize()
    original = ("ap-northeast-2", "/team/logs", "team-a")
    store.bind_cloudwatch_destination(*original)
    restarted = LogStore(path, cloudwatch=True)
    restarted.initialize()
    with pytest.raises(CloudWatchBindingError, match="destination changed") as error:
        restarted.bind_cloudwatch_destination(*destination)
    assert "Stored=" in str(error.value)
    assert "requested=" in str(error.value)
    store.bind_cloudwatch_destination(*original)


def test_legacy_unbound_cloudwatch_acks_require_new_database(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db", cloudwatch=True)
    store.initialize()
    event = example_event()
    _ = store.save_prediction(event)
    store.acknowledge(event.event_id)
    with pytest.raises(CloudWatchBindingError, match="no destination binding"):
        store.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", "team-a")
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM log_metadata").fetchone() == (0,)
    assert store.get_prediction(event.prediction_id).log_status == "cloudwatch_sent"


def test_other_backend_ack_does_not_prevent_cloudwatch_first_binding(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    simulated = LogStore(path, simulated=True)
    simulated.initialize()
    event = example_event()
    _ = simulated.save_prediction(event)
    simulated.acknowledge(event.event_id)
    cloudwatch = LogStore(path, cloudwatch=True)
    cloudwatch.initialize()
    cloudwatch.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", "team-a")
    assert cloudwatch.get_prediction(event.prediction_id).log_status == "pending"
    assert len(cloudwatch.pending()) == 1


def test_concurrent_first_binding_only_accepts_one_destination(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = LogStore(path, cloudwatch=True)
    store.initialize()

    def bind(stream: str) -> bool:
        try:
            LogStore(path, cloudwatch=True).bind_cloudwatch_destination(
                "ap-northeast-2", "/team/logs", stream
            )
        except CloudWatchBindingError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bind, ["team-a", "team-b"]))
    assert sorted(results) == [False, True]


def test_corrupt_metadata_is_never_replaced(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db", cloudwatch=True)
    store.initialize()
    with store.connection() as connection:
        _ = connection.execute(
            "INSERT INTO log_metadata(key, value) VALUES (?, ?)",
            (CLOUDWATCH_DESTINATION_KEY, "not-json"),
        )
    with pytest.raises(CloudWatchBindingError, match="Invalid CloudWatch destination metadata"):
        store.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", "team-a")


def test_blank_destination_is_never_bound(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "events.db", cloudwatch=True)
    store.initialize()
    with pytest.raises(CloudWatchBindingError, match="non-empty"):
        store.bind_cloudwatch_destination("ap-northeast-2", "/team/logs", " ")
