"""RQ job callables for assembly and QC workers."""

from __future__ import annotations

import os
import socket

from web.orchestrator import on_step_finished
from web import step_runners
from web.step_store import claim_step, finish_step, heartbeat_job, is_infra_error


def worker_id() -> str:
    return "{}:{}".format(socket.gethostname(), os.getpid())


def _run_claimed(step_id: str, expected_worker_type: str) -> str:
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
    job = step_runners.load_job(str(claimed["job_id"]))
    os.environ["_ASSEMBLY_JOB_ID"] = str(claimed["job_id"])
    try:
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
        finish_step(step_id, status="succeeded", exit_code=0)
        on_step_finished(step_id, "succeeded")
        return "succeeded"
    except Exception as error:  # noqa: BLE001
        message = "{}".format(error)
        finish_step(step_id, status="failed", error=message, exit_code=1)
        on_step_finished(step_id, "failed")
        if is_infra_error(message):
            raise
        return "failed"


def run_qc_step(step_id: str) -> str:
    return _run_claimed(step_id, "qc")


def run_assembly_step(step_id: str) -> str:
    return _run_claimed(step_id, "assembly")
