import os
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workload.api import STATIC_PATH, MetadataResponse, create_app
from workload.log_delivery import LogSettings
from workload.log_models import RecordedForecast
from workload.schemas import ForecastRequest, ForecastResponse
from workload.web_assets import mount_dashboard


def test_dashboard_sample_flows_through_real_model(tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        sample = client.get("/example")
        assert sample.status_code == 200
        request = ForecastRequest.model_validate_json(sample.text)
        response = client.post(
            "/predict",
            content=request.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        actual = RecordedForecast.model_validate_json(response.text)
        expected = ForecastResponse.model_validate_json(
            Path("examples/response.json").read_text(encoding="utf-8")
        )
        assert actual.model_version == expected.model_version
        assert actual.target_hour == expected.target_hour
        assert actual.workload_stage == expected.workload_stage
        assert actual.stage_range == expected.stage_range
        assert actual.probabilities == pytest.approx(expected.probabilities, abs=1e-12)
        metadata = MetadataResponse.model_validate_json(client.get("/metadata").text)
        assert metadata.history_hours == len(request.history)
        assert metadata.logging_enabled is True


@pytest.mark.parametrize(
    ("url", "media_type"),
    [
        ("/", "text/html"),
        ("/log-console", "text/html"),
        ("/static/styles.css", "text/css"),
        ("/static/logs.css", "text/css"),
        ("/static/app.mjs", "javascript"),
        ("/static/input.mjs", "javascript"),
        ("/static/feedback.mjs", "javascript"),
        ("/static/logs.mjs", "javascript"),
    ],
)
def test_dashboard_assets_are_browser_loadable(url: str, media_type: str, tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert media_type in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"


def test_dashboard_does_not_expose_repository_files(tmp_path: Path) -> None:
    with TestClient(
        create_app(log_settings=LogSettings(database=tmp_path / "events.db"))
    ) as client:
        assert client.get("/static/%2e%2e/artifact.py").status_code == 404
        assert client.get("/artifacts/model.json").status_code == 404


def asset_client(directory: Path) -> TestClient:
    application = FastAPI()
    mount_dashboard(application, directory)
    return TestClient(application)


def module_url(client: TestClient, page: str, filename: str) -> str:
    response = client.get(page)
    assert response.headers["cache-control"] == "no-store"
    match = re.search(rf'src="(/assets/[0-9a-f]{{64}}/{re.escape(filename)})"', response.text)
    assert match is not None
    return match[1]


def test_dependency_change_versions_entire_module_graph_even_with_same_size_mtime(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _ = shutil.copytree(STATIC_PATH, first)
    _ = shutil.copytree(first, second)
    dependency = second / "input.mjs"
    before = dependency.stat()
    original = dependency.read_bytes()
    updated = original.replace("정각".encode(), "시각".encode(), 1)
    assert original != updated
    _ = dependency.write_bytes(updated)
    os.utime(dependency, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert dependency.stat().st_size == before.st_size
    assert dependency.stat().st_mtime_ns == before.st_mtime_ns
    assert (first / "app.mjs").read_bytes() == (second / "app.mjs").read_bytes()
    with asset_client(first) as old, asset_client(second) as new:
        old_entry = module_url(old, "/", "app.mjs")
        new_entry = module_url(new, "/", "app.mjs")
        assert new_entry != old_entry
        new_prefix = new_entry.rsplit("/", 1)[0]
        for name in ("app.mjs", "feedback.mjs"):
            module = new.get(f"{new_prefix}/{name}")
            assert module.status_code == 200
            for dependency_match in re.finditer(r'from "(\./[^\"]+\.mjs)"', module.text):
                relative = dependency_match.group(1)
                imported = urljoin(f"{new_prefix}/{name}", relative)
                assert imported.startswith(new_prefix + "/")
                assert new.get(imported).status_code == 200
        assert new.get(urljoin(new_entry, "./input.mjs")).content == updated
        assert new.get(old_entry).status_code == 404
        assert new.get(f"{new_prefix}/%2e%2e/artifact.py").status_code == 404
        assert module_url(new, "/log-console", "logs.mjs").startswith(new_prefix + "/")


@pytest.mark.parametrize("versioned", [False, True])
def test_static_assets_do_not_reuse_legacy_file_metadata_validators(*, versioned: bool) -> None:
    with asset_client(STATIC_PATH) as client:
        path = module_url(client, "/", "app.mjs") if versioned else "/static/app.mjs"
        response = client.get(
            path,
            headers={"If-None-Match": "*", "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT"},
        )
        assert response.status_code == 200
        assert response.content == (STATIC_PATH / "app.mjs").read_bytes()
        assert response.headers["cache-control"] == "no-store"
        assert "etag" not in response.headers
        assert "last-modified" not in response.headers
