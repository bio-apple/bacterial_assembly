"""Cancel RQ-backed jobs: mark PG cancelled and stop tool containers."""

from __future__ import annotations

from web.pg import connection
from web.step_store import utc_now
from web.task_backend import use_rq_pipeline


def cancel_rq_job(job_id: str) -> None:
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
                    cancel_requested_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (now, now, job_id),
            )
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'cancelled',
                    completed_at = %s,
                    updated_at = %s,
                    error = COALESCE(error, 'job cancelled')
                WHERE job_id = %s AND status IN ('pending', 'queued', 'running')
                """,
                (now, now, job_id),
            )
            cur.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', completed_at = %s, updated_at = %s
                WHERE id = %s AND status = 'cancelling'
                """,
                (now, now, job_id),
            )
    try:
        from scripts.docker_executor import get_executor, tool_runtime

        if tool_runtime() == "docker":
            get_executor().stop_job_containers(str(job_id))
    except Exception:
        pass
