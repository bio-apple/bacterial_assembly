"""RQ queue helpers."""

from __future__ import annotations

import os
from functools import lru_cache

ASSEMBLY_QUEUE = "assembly"
QC_QUEUE = "qc"


def redis_url() -> str:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is not set")
    return url


@lru_cache(maxsize=1)
def redis_connection():
    try:
        from redis import Redis
    except ImportError as error:
        raise RuntimeError("redis package is required; pip install redis") from error
    return Redis.from_url(redis_url())


def queue(name: str):
    try:
        from rq import Queue
    except ImportError as error:
        raise RuntimeError("rq package is required; pip install rq") from error
    return Queue(name, connection=redis_connection())


def enqueue_step(step_id: str, worker_type: str, *, job_timeout: int = 86400):
    from web import rq_jobs
    from web.step_store import enqueue_ready

    step = enqueue_ready(step_id)
    if worker_type == "assembly":
        qname = ASSEMBLY_QUEUE
        func = rq_jobs.run_assembly_step
    elif worker_type == "qc":
        qname = QC_QUEUE
        func = rq_jobs.run_qc_step
    else:
        raise ValueError("worker_type must be assembly or qc")
    rq_job_id = "step-{}-{}".format(step_id, step.get("attempt") or 0)
    return queue(qname).enqueue(
        func,
        step_id,
        job_id=rq_job_id,
        job_timeout=job_timeout,
        result_ttl=86400,
        failure_ttl=86400,
        meta={"step_id": step_id, "step_name": step.get("step_name")},
    )
