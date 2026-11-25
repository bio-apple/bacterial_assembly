"""Task execution backend selection."""

from __future__ import annotations

import os


def task_backend() -> str:
    value = os.environ.get("TASK_BACKEND", "sqlite").strip().lower()
    if value in {"", "sqlite", "legacy"}:
        return "sqlite"
    if value in {"postgres_rq", "rq", "postgres"}:
        return "postgres_rq"
    raise RuntimeError("TASK_BACKEND must be sqlite or postgres_rq")


def use_rq_pipeline() -> bool:
    return task_backend() == "postgres_rq"
