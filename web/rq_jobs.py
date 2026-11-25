"""RQ job callables for assembly and QC workers."""

from __future__ import annotations

import os
import socket

from scripts.resource_budget import read_runtime_limits
from web.orchestrator import on_step_finished
from web import step_runners
from web.sqlite_mirror import mirror_job_status, sync_sqlite_steps_from_pg
from web.step_store import (
    claim_step,
    finish_step,
    heartbeat_job,
    is_infra_error,
    job_cancel_requested,
    reclaim_stale_running_steps,
)


def worker_id() -> str:
    return "{}:{}".format(socket.gethostname(), os.getpid())


def _apply_resource_env(job: dict) -> None:
    try:
        budget = read_runtime_limits()
        memory_gib = int(budget["method_memory_gib"])
    except (RuntimeError, TypeError, ValueError, KeyError):
        memory_gib = None
    threads = int(job.get("threads") or 1)
    os.environ["_ASSEMBLY_ASSIGNED_THREADS"] = str(threads)
    os.environ["_ASSEMBLY_JOB_ID"] = str(job["id"])
    if memory_gib is not None:
        os.environ["_ASSEMBLY_METHOD_MEMORY_GIB"] = str(memory_gib)
        job["_memory_gib"] = memory_gib


def _run_claimed(step_id: str, expected_worker_type: str) -> str:
    reclaim_stale_running_steps()
    claimed = claim_step(step_id, worker_id())
    if claimed is None:
        return "skipped"
    if claimed.get("worker_type") and claimed["worker_type"] != expected_worker_type:
        finish_step(
            step_id,
            status="failed",
            error="step worker_type {} does not match {}".format(
                claimed.get("worker_type"), expected_worker_type
            ),
        )
        on_step_finished(step_id, "failed")
        return "failed"
    heartbeat_job(str(claimed["job_id"]), worker_id())
    mirror_job_status(str(claimed["job_id"]), "running", terminal=False)
    job = step_runners.load_job(str(claimed["job_id"]))
    _apply_resource_env(job)
    os.environ["_ASSEMBLY_STEP_ID"] = str(step_id)
    os.environ["_ASSEMBLY_STEP_NAME"] = str(claimed.get("step_name") or "")
    try:
        if job_cancel_requested(str(claimed["job_id"])):
            finish_step(step_id, status="cancelled", error="job cancelled")
            on_step_finished(step_id, "cancelled")
            return "cancelled"
        step_type = claimed["step_type"]
        if step_type == "noop":
            pass
        elif step_type == "fastp":
            step_runners.run_fastp_step(job, claimed)
        elif step_type == "assembly":
            step_runners.run_assembly_step(job, claimed)
        elif step_type == "quast":
            step_runners.run_quast_step(job, claimed)
        elif step_type == "summary":
            step_runners.run_summary_step(job, claimed)
        else:
            raise RuntimeError("unknown step_type {}".format(step_type))
        if job_cancel_requested(str(claimed["job_id"])):
            finish_step(step_id, status="cancelled", error="job cancelled")
            on_step_finished(step_id, "cancelled")
            sync_sqlite_steps_from_pg(str(claimed["job_id"]))
            return "cancelled"
        finish_step(step_id, status="succeeded", exit_code=0)
        on_step_finished(step_id, "succeeded")
        sync_sqlite_steps_from_pg(str(claimed["job_id"]))
        return "succeeded"
    except Exception as error:  # noqa: BLE001
        message = "{}".format(error)
        finish_step(step_id, status="failed", error=message, exit_code=1)
        on_step_finished(step_id, "failed")
        sync_sqlite_steps_from_pg(str(claimed["job_id"]))
        if is_infra_error(message):
            raise
        return "failed"


def run_qc_step(step_id: str) -> str:
    return _run_claimed(step_id, "qc")


def run_assembly_step(step_id: str) -> str:
    return _run_claimed(step_id, "assembly")
