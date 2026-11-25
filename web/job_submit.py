"""Shared adapter: enqueue an analysis job on the Postgres/RQ pipeline."""

from __future__ import annotations

import logging
import sqlite3

from web.orchestrator import plan_and_insert_job, start_job
from web.sqlite_mirror import mirror_job_status
from web.task_backend import use_rq_pipeline

logger = logging.getLogger(__name__)


def enqueue_rq_pipeline(
    *,
    job_id: str,
    owner: str,
    sample: str,
    assemblers: list[str],
    threads: int,
    spades_kmers: str | None,
    spades_careful: bool,
    input_r1: str,
    input_r2: str,
    output_dir: str,
    log_path: str,
) -> bool:
    """
    Plan PG steps and start the first QC work item when TASK_BACKEND=postgres_rq.
    Returns True if the RQ path was used.
    """
    if not use_rq_pipeline():
        return False
    plan_and_insert_job(
        job_id=job_id,
        owner=owner,
        sample=sample,
        assemblers=assemblers,
        threads=threads,
        spades_kmers=spades_kmers,
        spades_careful=spades_careful,
        input_r1=input_r1,
        input_r2=input_r2,
        output_dir=output_dir,
        log_path=log_path,
    )
    start_job(job_id)
    return True


def compensate_failed_enqueue(job_id: str, error: str = "Could not start analysis queue") -> None:
    """Mark SQLite (+ best-effort PG) failed when enqueue fails after catalog insert."""
    try:
        from flask import current_app

        database = current_app.config.get("DATABASE")
    except Exception:
        database = None
    if database is not None:
        try:
            db = sqlite3.connect(database, timeout=30)
            try:
                db.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                    "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ?",
                    (error, job_id),
                )
                db.commit()
            finally:
                db.close()
        except sqlite3.Error:
            logger.exception("Could not compensate SQLite job %s", job_id)
    mirror_job_status(job_id, "failed", error)
    if use_rq_pipeline():
        try:
            from web.pg import connection
            from web.step_store import utc_now

            now = utc_now()
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', error = %s, completed_at = %s, updated_at = %s
                        WHERE id = %s AND status IN ('queued', 'running', 'cancelling')
                        """,
                        (error, now, now, job_id),
                    )
        except Exception:
            logger.exception("Could not compensate Postgres job %s", job_id)


def submit_analysis_task(
    *,
    job_id: str,
    owner: str,
    sample: str,
    assemblers: list[str],
    threads: int,
    spades_kmers: str | None,
    spades_careful: bool,
    input_r1: str,
    input_r2: str,
    output_dir: str,
    log_path: str,
) -> bool:
    """
    After SQLite catalog insert: plan PG + enqueue, with unified compensation on failure.
    Returns True if RQ pipeline was used.
    """
    try:
        return enqueue_rq_pipeline(
            job_id=job_id,
            owner=owner,
            sample=sample,
            assemblers=assemblers,
            threads=threads,
            spades_kmers=spades_kmers,
            spades_careful=spades_careful,
            input_r1=input_r1,
            input_r2=input_r2,
            output_dir=output_dir,
            log_path=log_path,
        )
    except Exception:
        logger.exception("submit_analysis_task failed for %s", job_id)
        compensate_failed_enqueue(job_id)
        raise
