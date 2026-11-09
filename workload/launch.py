"""Launch isolated local or authenticated AWS logging previews on localhost."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Protocol, cast

import boto3
import typer
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
MAX_CLOUDWATCH_NAME_LENGTH = 512
AUTHENTICATION_TIMEOUT_SECONDS = 30
app = typer.Typer(no_args_is_help=True)
Port = Annotated[int, typer.Option(min=1, max=65535)]
Model = Annotated[Path | None, typer.Option(exists=True, dir_okay=False, readable=True)]


class IdentityClient(Protocol):
    def get_caller_identity(self) -> object: ...


def safe_error(error: Exception) -> str:
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            return code
    return type(error).__name__


def check_identity() -> int:
    """Run in a child process so login-cache environment changes remain isolated."""
    try:
        session = boto3.Session(
            profile_name=os.environ["AWS_PROFILE"], region_name=os.environ["AWS_REGION"]
        )
        client = cast(
            "IdentityClient",
            session.client(  # pyright: ignore[reportUnknownMemberType]
                "sts",
                config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}),
            ),
        )
        _ = client.get_caller_identity()
    except Exception as error:  # noqa: BLE001 -- Never print credential-bearing exception text.
        typer.echo(safe_error(error))
        return 1
    return 0


def run_server(environment: dict[str, str], port: int) -> None:
    try:
        result = subprocess.run(  # noqa: S603 -- Fixed executable/module; no shell.
            [
                sys.executable,
                "-m",
                "uvicorn",
                "workload.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError as error:
        typer.echo(safe_error(error), err=True)
        raise typer.Exit(1) from None
    raise typer.Exit(result.returncode)


def model_environment(environment: dict[str, str], model: Path | None) -> None:
    # Resolve before the child switches cwd, and override inherited model selection.
    environment["WORKLOAD_MODEL_PATH"] = str(
        (model if model is not None else ROOT / "artifacts/model.json").resolve()
    )


@app.command()
def local(port: Port = 8000, model: Model = None) -> None:
    """Start local S3 simulation without making AWS calls."""
    environment = dict(os.environ)
    environment.update(
        WORKLOAD_LOG_BACKEND="local-s3",
        WORKLOAD_LOG_DB=str(ROOT / "runtime/local-preview/events.sqlite3"),
        WORKLOAD_LOCAL_S3_ROOT=str(ROOT / "runtime/local-preview/objects"),
        WORKLOAD_S3_BUCKET="",
        WORKLOAD_S3_PREFIX="workload-events",
    )
    model_environment(environment, model)
    run_server(environment, port)


@app.command()
def aws(  # noqa: PLR0913 -- Typer exposes one parameter per command-line option.
    bucket: Annotated[str, typer.Option(help="Existing S3 bucket name; never created here.")],
    profile: Annotated[str, typer.Option()] = "workload",
    region: Annotated[str, typer.Option()] = "ap-northeast-2",
    port: Port = 8000,
    model: Model = None,
    *,
    project_login: Annotated[bool, typer.Option("--project-login")] = False,
) -> None:
    """Verify AWS identity, then start real S3 delivery using a separate database."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        message = "Provide a bucket name without spaces or a URL."
        raise typer.BadParameter(message, param_hint="--bucket")
    environment = dict(os.environ)
    environment.update(
        AWS_PROFILE=profile,
        AWS_REGION=region,
        WORKLOAD_LOG_BACKEND="s3",
        WORKLOAD_LOG_DB=str(ROOT / "runtime/aws-live/events.sqlite3"),
        WORKLOAD_S3_BUCKET=bucket,
        WORKLOAD_S3_PREFIX="workload-events",
    )
    model_environment(environment, model)
    authenticate_and_run(environment, port, project_login=project_login)


@app.command()
def cloudwatch(  # noqa: PLR0913 -- Typer exposes one parameter per command-line option.
    log_group: Annotated[str, typer.Option(help="Existing CloudWatch log group.")],
    log_stream: Annotated[str, typer.Option(help="Existing CloudWatch log stream.")],
    *,
    profile: Annotated[str, typer.Option()] = "workload",
    region: Annotated[str, typer.Option()] = "ap-northeast-2",
    port: Port = 8000,
    model: Model = None,
    project_login: Annotated[bool, typer.Option("--project-login")] = False,
) -> None:
    """Send logs to an existing CloudWatch group/stream after checking identity."""
    if not re.fullmatch(r"[.\-_/#A-Za-z0-9]{1,512}", log_group):
        message = "Provide an existing CloudWatch log group name."
        raise typer.BadParameter(message, param_hint="--log-group")
    if (
        not log_stream.strip()
        or len(log_stream) > MAX_CLOUDWATCH_NAME_LENGTH
        or any(character in log_stream for character in ":*\r\n")
    ):
        message = "Provide an existing CloudWatch log stream name."
        raise typer.BadParameter(message, param_hint="--log-stream")
    environment = dict(os.environ)
    environment.update(
        AWS_PROFILE=profile,
        AWS_REGION=region,
        WORKLOAD_LOG_BACKEND="cloudwatch",
        WORKLOAD_LOG_DB=str(ROOT / "runtime/cloudwatch-live/events.sqlite3"),
        WORKLOAD_CLOUDWATCH_LOG_GROUP=log_group,
        WORKLOAD_CLOUDWATCH_LOG_STREAM=log_stream,
        WORKLOAD_S3_BUCKET="",
    )
    model_environment(environment, model)
    authenticate_and_run(environment, port, project_login=project_login)


def authenticate_and_run(environment: dict[str, str], port: int, *, project_login: bool) -> None:
    if project_login:
        login_root = ROOT / "runtime/aws"
        if not (login_root / "config").is_file():
            typer.echo("ProjectLoginConfigMissing", err=True)
            raise typer.Exit(1)
        environment.update(
            AWS_CONFIG_FILE=str(login_root / "config"),
            AWS_SHARED_CREDENTIALS_FILE=str(login_root / "credentials"),
            AWS_LOGIN_CACHE_DIRECTORY=str(login_root / "login-cache"),
        )
    try:
        identity = subprocess.run(
            [
                sys.executable,
                "-c",
                "from workload.launch import check_identity; raise SystemExit(check_identity())",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=AUTHENTICATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        typer.echo("AuthenticationTimeout", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError as error:
        typer.echo(safe_error(error), err=True)
        raise typer.Exit(1) from None
    if identity.returncode:
        code = identity.stdout.strip()
        typer.echo(
            code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "AuthenticationFailed",
            err=True,
        )
        raise typer.Exit(identity.returncode)
    run_server(environment, port)


if __name__ == "__main__":
    app()
