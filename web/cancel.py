"""任务取消 port for postgres_rq (ADR 0014 / 0020)."""

from __future__ import annotations

from web.pg import connection
from web.sqlite_mirror import mirror_job_status, sync_sqlite_steps_from_pg
from web.step_store import utc_now
from web.task_backend import use_rq_pipeline


def request_cancel(job_id: str, *, mark_sqlite_cancelled: bool = False) -> None:
    """
    cancelling → stop containers / cancel open steps → cancelled.
    Queued jobs may already be cancelled in SQLite; still sync PG + stop any containers.
    """
    if not use_rq_pipeline():
        return
    now = utc_now()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN status IN ('queued', 'running') THEN 'cancelling'
                        ELSE status
                    END,
                    cancel_requested = TRUE,
                    cancel_requested_at = COALESCE(cancel_requested_at, %s),
                    updated_at = %s
                WHERE id = %s
                """,
                (now, now, job_id),
            )
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'cancelled',
                    completed_at = COALESCE(completed_at, %s),
                    updated_at = %s,
                    error = COALESCE(error, 'job cancelled')
                WHERE job_id = %s AND status IN ('pending', 'queued', 'running')
                """,
                (now, now, job_id),
            )
    # Stop tool containers while job is still cancelling (not yet terminal).
    _stop_containers(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', completed_at = COALESCE(completed_at, %s), updated_at = %s
                WHERE id = %s AND status = 'cancelling'
                """,
                (now, now, job_id),
            )
    if mark_sqlite_cancelled:
        mirror_job_status(job_id, "cancelled", "job cancelled")
    else:
        mirror_job_status(job_id, "cancelled", "job cancelled")
    sync_sqlite_steps_from_pg(job_id)


def _stop_containers(job_id: str) -> None:
    try:
        from scripts.docker_executor import get_executor, tool_runtime

        if tool_runtime() == "docker":
            get_executor().stop_job_containers(str(job_id))
    except Exception:
        pass


# Back-compat name used by older call sites / tests
def cancel_rq_job(job_id: str) -> None:
    request_cancel(job_id)
