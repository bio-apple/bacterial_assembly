"""Validate 应用运行时 environment at process start (ADR 0020)."""

from __future__ import annotations

import os


class RuntimeConfigError(RuntimeError):
    pass


def task_backend() -> str:
    return os.environ.get("TASK_BACKEND", "sqlite").strip().lower() or "sqlite"


def tool_runtime_value() -> str:
    value = os.environ.get("TOOL_RUNTIME", "local").strip().lower()
    if value in {"", "local"}:
        return "local"
    if value == "docker":
        return "docker"
    raise RuntimeConfigError(
        "TOOL_RUNTIME must be 'docker' or 'local', got {!r}".format(value)
    )


def validate_worker_runtime(*, check_docker: bool = True) -> None:
    """Fail fast for RQ worker processes."""
    backend = task_backend()
    if backend in {"postgres_rq", "rq", "postgres"}:
        if not os.environ.get("DATABASE_URL", "").strip():
            raise RuntimeConfigError("DATABASE_URL is required when TASK_BACKEND=postgres_rq")
        if not os.environ.get("REDIS_URL", "").strip():
            raise RuntimeConfigError("REDIS_URL is required when TASK_BACKEND=postgres_rq")
    runtime = tool_runtime_value()
    if runtime == "docker" and check_docker:
        _ping_docker()


def validate_web_runtime(*, check_docker: bool = False) -> None:
    """Fail fast for Flask/Gunicorn (secret always; Docker optional until cancel)."""
    secret = os.environ.get("ASSEMBLY_WEB_SECRET", "").strip()
    # create_app also enforces length; keep a light check here for workers sharing module
    backend = task_backend()
    if backend in {"postgres_rq", "rq", "postgres"}:
        if not os.environ.get("DATABASE_URL", "").strip():
            raise RuntimeConfigError("DATABASE_URL is required when TASK_BACKEND=postgres_rq")
        if not os.environ.get("REDIS_URL", "").strip():
            raise RuntimeConfigError("REDIS_URL is required when TASK_BACKEND=postgres_rq")
    runtime = tool_runtime_value()
    if runtime == "docker" and check_docker:
        _ping_docker()
    if secret is not None and secret == "" and os.environ.get("FLASK_ENV") == "production":
        raise RuntimeConfigError("ASSEMBLY_WEB_SECRET is required")


def _ping_docker() -> None:
    try:
        import docker
    except ImportError as error:
        raise RuntimeConfigError("docker package required when TOOL_RUNTIME=docker") from error
    try:
        docker.from_env().ping()
    except Exception as error:
        raise RuntimeConfigError("Docker Engine unreachable (TOOL_RUNTIME=docker)") from error


def wire_tool_runtime_hooks() -> None:
    """Inject CancelPoll / set_container adapters into scripts.docker_executor."""
    try:
        from scripts.docker_executor import set_cancel_poll, set_container_recorder
        from web.step_store import job_cancel_requested, set_container

        set_cancel_poll(job_cancel_requested)
        set_container_recorder(set_container)
    except Exception:
        pass
