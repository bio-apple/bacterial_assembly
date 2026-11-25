"""Shared adapter: enqueue an analysis job on the Postgres/RQ pipeline."""

from __future__ import annotations

from web.orchestrator import plan_and_insert_job, start_job
from web.task_backend import use_rq_pipeline


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
