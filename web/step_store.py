"""PostgreSQL step state machine helpers (idempotency + claim)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from web.pg import connection

INFRA_ERROR_MARKERS = (
    "docker engine is unavailable",
    "connection refused",
    "temporarily unavailable",
    "broken pipe",
    "network is unreachable",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def is_infra_error(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in INFRA_ERROR_MARKERS)


def get_step(cur, step_id: str) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM job_steps WHERE id = %s", (step_id,))
    return cur.fetchone()


def get_job(cur, job_id: str) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    return cur.fetchone()


def claim_step(step_id: str, worker_id: str) -> dict[str, Any] | None:
    """
    Atomically move step to running if it may start.
    Returns None if the step should not run (already done / cancelled / running elsewhere).
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM job_steps WHERE id = %s FOR UPDATE",
                (step_id,),
            )
            step = cur.fetchone()
            if step is None:
                raise RuntimeError("unknown step_id {}".format(step_id))
            cur.execute(
                "SELECT status, cancel_requested FROM jobs WHERE id = %s FOR UPDATE",
                (step["job_id"],),
            )
            job = cur.fetchone()
            if job is None:
                raise RuntimeError("unknown job_id {}".format(step["job_id"]))
            if job["status"] in {"cancelling", "cancelled"} or job["cancel_requested"]:
                cur.execute(
                    """
                    UPDATE job_steps
                    SET status = 'cancelled',
                        updated_at = %s,
                        completed_at = %s,
                        error = COALESCE(error, 'job cancelled')
                    WHERE id = %s AND status IN ('pending', 'queued', 'running')
                    """,
                    (utc_now(), utc_now(), step_id),
                )
                return None
            if step["status"] == "succeeded":
                return None
            if step["status"] == "running":
                # Another worker owns it; caller should inspect container_id.
                return None
            if step["status"] in {"cancelled", "skipped"}:
                return None
            if step["status"] == "failed" and step["attempt"] >= step["max_attempts"]:
                return None
            if step["status"] == "failed" and not is_infra_error(step.get("error") or ""):
                return None

            now = utc_now()
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'running',
                    attempt = attempt + 1,
                    started_at = COALESCE(started_at, %s),
                    updated_at = %s,
                    error = NULL,
                    exit_code = NULL
                WHERE id = %s
                RETURNING *
                """,
                (now, now, step_id),
            )
            claimed = cur.fetchone()
            cur.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN status IN ('queued', 'failed') THEN 'running'
                        ELSE status
                    END,
                    started_at = COALESCE(started_at, %s),
                    updated_at = %s,
                    heartbeat_at = %s,
                    worker_id = %s
                WHERE id = %s
                """,
                (now, now, now, worker_id, step["job_id"]),
            )
            return claimed


def finish_step(
    step_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error: str | None = None,
    container_id: str | None = None,
    image_ref: str | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "cancelled", "skipped"}:
        raise ValueError("invalid terminal step status")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_steps
                SET status = %s,
                    exit_code = %s,
                    error = %s,
                    container_id = COALESCE(%s, container_id),
                    image_ref = COALESCE(%s, image_ref),
                    log_path = COALESCE(%s, log_path),
                    completed_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    status,
                    exit_code,
                    error,
                    container_id,
                    image_ref,
                    log_path,
                    utc_now(),
                    utc_now(),
                    step_id,
                ),
            )
            step = cur.fetchone()
            if step is None:
                raise RuntimeError("unknown step_id {}".format(step_id))
            return step


def set_container(step_id: str, container_id: str, image_ref: str | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_steps
                SET container_id = %s,
                    image_ref = COALESCE(%s, image_ref),
                    updated_at = %s
                WHERE id = %s
                """,
                (container_id, image_ref, utc_now(), step_id),
            )


def heartbeat_job(job_id: str, worker_id: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET heartbeat_at = %s, worker_id = %s, updated_at = %s
                WHERE id = %s
                """,
                (utc_now(), worker_id, utc_now(), job_id),
            )


def enqueue_ready(step_id: str) -> dict[str, Any]:
    """Mark step queued before pushing to Redis."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'queued', updated_at = %s
                WHERE id = %s AND status IN ('pending', 'failed')
                RETURNING *
                """,
                (utc_now(), step_id),
            )
            step = cur.fetchone()
            if step is None:
                cur.execute("SELECT * FROM job_steps WHERE id = %s", (step_id,))
                step = cur.fetchone()
                if step is None:
                    raise RuntimeError("unknown step_id {}".format(step_id))
            return step


def create_demo_job_with_noop(*, owner: str = "demo") -> tuple[str, str]:
    """Insert a minimal job + noop step for Phase 3 smoke."""
    job_id = new_id()
    step_id = new_id()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id, owner, status, assemblers, threads, sample,
                    spades_careful, input_r1, input_r2, output_dir, log_path
                ) VALUES (
                    %s, %s, 'queued', %s, 1, 'demo',
                    FALSE, '/data/demo/r1.fq', '/data/demo/r2.fq',
                    %s, %s
                )
                """,
                (
                    job_id,
                    owner,
                    '["noop"]',
                    "/data/jobs/{}/work".format(job_id),
                    "/data/jobs/{}/logs/job.log".format(job_id),
                ),
            )
            cur.execute(
                """
                INSERT INTO job_steps (
                    id, job_id, step_name, step_type, software, status, worker_type
                ) VALUES (
                    %s, %s, 'noop', 'noop', 'noop', 'pending', 'qc'
                )
                """,
                (step_id, job_id),
            )
    return job_id, step_id
