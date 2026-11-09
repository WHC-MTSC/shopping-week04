import os
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from workload import launch

runner = CliRunner()


def test_local_isolated_from_inherited_aws_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setenv("WORKLOAD_LOG_BACKEND", "s3")
    monkeypatch.setenv("WORKLOAD_S3_BUCKET", "real-bucket")
    before = dict(os.environ)
    with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
        result = runner.invoke(launch.app, ["local", "--port", "8123"])
    assert result.exit_code == 0
    assert run.call_count == 1
    arguments = run.call_args
    assert arguments.args[0][-4:] == ["--host", "127.0.0.1", "--port", "8123"]
    environment = cast("dict[str, str]", arguments.kwargs["env"])
    assert environment["WORKLOAD_LOG_BACKEND"] == "local-s3"
    assert environment["WORKLOAD_S3_BUCKET"] == ""
    assert environment["WORKLOAD_LOG_DB"] == str(tmp_path / "runtime/local-preview/events.sqlite3")
    assert environment["WORKLOAD_LOCAL_S3_ROOT"] == str(tmp_path / "runtime/local-preview/objects")
    assert arguments.kwargs["cwd"] == tmp_path
    assert dict(os.environ) == before
    assert not (tmp_path / "runtime").exists()


def test_aws_authenticates_before_server_and_preserves_normal_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    before = dict(os.environ)
    with patch.object(
        subprocess,
        "run",
        side_effect=[subprocess.CompletedProcess([], 0, ""), subprocess.CompletedProcess([], 7)],
    ) as run:
        result = runner.invoke(
            launch.app, ["aws", "--bucket", "existing-bucket", "--profile", "personal"]
        )
    assert result.exit_code == 7
    assert run.call_count == 2
    auth, server = run.call_args_list
    assert "check_identity" in auth.args[0][-1]
    assert server.args[0][1:4] == ["-m", "uvicorn", "workload.api:app"]
    assert auth.kwargs["env"] == server.kwargs["env"]
    assert auth.kwargs["timeout"] == launch.AUTHENTICATION_TIMEOUT_SECONDS
    assert "timeout" not in server.kwargs
    environment = cast("dict[str, str]", server.kwargs["env"])
    assert environment["AWS_PROFILE"] == "personal"
    assert environment["AWS_REGION"] == "ap-northeast-2"
    assert environment["WORKLOAD_LOG_BACKEND"] == "s3"
    assert environment["WORKLOAD_S3_BUCKET"] == "existing-bucket"
    assert environment["WORKLOAD_LOG_DB"] == str(tmp_path / "runtime/aws-live/events.sqlite3")
    assert environment.get("AWS_CONFIG_FILE") == before.get("AWS_CONFIG_FILE")
    assert dict(os.environ) == before


def test_project_login_only_changes_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    config = tmp_path / "runtime/aws/config"
    config.parent.mkdir(parents=True)
    _ = config.write_text("[profile workload]\n", encoding="utf-8")
    before = dict(os.environ)
    with patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")
    ) as run:
        result = runner.invoke(
            launch.app, ["aws", "--bucket", "existing-bucket", "--project-login"]
        )
    assert result.exit_code == 0
    environment = cast("dict[str, str]", run.call_args.kwargs["env"])
    assert environment["AWS_CONFIG_FILE"] == str(config)
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == str(config.parent / "credentials")
    assert environment["AWS_LOGIN_CACHE_DIRECTORY"] == str(config.parent / "login-cache")
    assert dict(os.environ) == before


@pytest.mark.parametrize("output", ["AccessDenied\n", "private details with spaces\n"])
def test_authentication_failure_never_starts_server_or_creates_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    with patch.object(
        subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 1, output, "private traceback"),
    ) as run:
        result = runner.invoke(launch.app, ["aws", "--bucket", "existing-bucket"])
    assert result.exit_code == 1
    assert run.call_count == 1
    assert "private" not in result.output
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["aws"],
        ["aws", "--bucket", ""],
        ["aws", "--bucket", " bad-bucket"],
        ["aws", "--bucket", "https://bucket"],
        ["aws", "--bucket", "valid-bucket", "--port", "0"],
        ["local", "--port", "65536"],
    ],
)
def test_invalid_arguments_fail_before_process(arguments: list[str]) -> None:
    with patch.object(subprocess, "run") as run:
        result = runner.invoke(launch.app, arguments)
    assert result.exit_code == 2
    run.assert_not_called()


def test_missing_project_config_fails_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    with patch.object(subprocess, "run") as run:
        result = runner.invoke(launch.app, ["aws", "--bucket", "valid-bucket", "--project-login"])
    assert result.exit_code == 1
    run.assert_not_called()


def test_ctrl_c_exits_gracefully() -> None:
    with patch.object(subprocess, "run", side_effect=KeyboardInterrupt):
        result = runner.invoke(launch.app, ["local"])
    assert result.exit_code == 130


@pytest.mark.parametrize("fail", [False, True])
def test_identity_calls_sts_and_sanitizes_failure(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "workload")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "private"}}, "GetCallerIdentity"
    )
    calls: list[str] = []

    class FakeIdentity:
        def get_caller_identity(self) -> object:
            calls.append("GetCallerIdentity")
            if fail:
                raise error
            return {}

    class FakeSession:
        def __init__(self, *, profile_name: str, region_name: str) -> None:
            assert profile_name == "workload"
            assert region_name == "ap-northeast-2"

        def client(self, service_name: str, *, config: Config) -> FakeIdentity:
            assert service_name == "sts"
            calls.append(type(config).__name__)
            return FakeIdentity()

    monkeypatch.setattr("workload.launch.boto3.Session", FakeSession)
    assert launch.check_identity() == int(fail)
    assert calls == ["Config", "GetCallerIdentity"]
    assert launch.safe_error(error) == "AccessDenied"


@pytest.mark.parametrize(
    "arguments",
    [
        ["local"],
        ["aws", "--bucket", "existing-bucket"],
        ["cloudwatch", "--log-group", "/team/workload", "--log-stream", "team-a"],
    ],
)
def test_model_selection_resolves_before_child_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    model = tmp_path / "candidate.json"
    _ = model.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKLOAD_MODEL_PATH", "old-model.json")
    with patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")
    ) as run:
        result = runner.invoke(launch.app, [*arguments, "--model", "candidate.json"])
    assert result.exit_code == 0
    environment = cast("dict[str, str]", run.call_args.kwargs["env"])
    assert environment["WORKLOAD_MODEL_PATH"] == str(model)


def test_cloudwatch_isolated_and_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setenv("WORKLOAD_S3_BUCKET", "wrong-target")
    with patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")
    ) as run:
        result = runner.invoke(
            launch.app,
            [
                "cloudwatch",
                "--log-group",
                "/team/workload",
                "--log-stream",
                "team-a",
                "--region",
                "eu-west-1",
            ],
        )
    assert result.exit_code == 0
    assert run.call_count == 2
    environment = cast("dict[str, str]", run.call_args.kwargs["env"])
    assert environment["WORKLOAD_LOG_BACKEND"] == "cloudwatch"
    assert environment["WORKLOAD_CLOUDWATCH_LOG_GROUP"] == "/team/workload"
    assert environment["WORKLOAD_CLOUDWATCH_LOG_STREAM"] == "team-a"
    assert environment["WORKLOAD_LOG_DB"] == str(
        tmp_path / "runtime/cloudwatch-live/events.sqlite3"
    )
    assert environment["AWS_REGION"] == "eu-west-1"
    assert environment["WORKLOAD_S3_BUCKET"] == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["cloudwatch"],
        ["cloudwatch", "--log-group", "valid", "--log-stream", ""],
        ["cloudwatch", "--log-group", "bad group", "--log-stream", "team"],
        ["cloudwatch", "--log-group", "valid", "--log-stream", "bad:stream"],
        ["local", "--model", "does-not-exist.json"],
    ],
)
def test_invalid_cloudwatch_or_model_fails_before_any_process(arguments: list[str]) -> None:
    with patch.object(subprocess, "run") as run:
        result = runner.invoke(launch.app, arguments)
    assert result.exit_code == 2
    run.assert_not_called()


def test_cloudwatch_authentication_failure_never_starts_server() -> None:
    with patch.object(
        subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "AccessDenied")
    ) as run:
        result = runner.invoke(
            launch.app, ["cloudwatch", "--log-group", "existing", "--log-stream", "team"]
        )
    assert result.exit_code == 1
    assert run.call_count == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["aws", "--bucket", "existing-bucket"],
        ["cloudwatch", "--log-group", "existing", "--log-stream", "team"],
    ],
)
def test_stalled_authentication_times_out_without_server_or_sensitive_output(
    arguments: list[str],
) -> None:
    failure = subprocess.TimeoutExpired(
        "private command", 30, output="private output", stderr="private error"
    )
    with patch.object(subprocess, "run", side_effect=failure) as run:
        result = runner.invoke(launch.app, arguments)
    assert result.exit_code == 1
    assert result.output.strip() == "AuthenticationTimeout"
    assert run.call_count == 1
    assert run.call_args.kwargs["timeout"] == launch.AUTHENTICATION_TIMEOUT_SECONDS
