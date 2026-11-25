"""Phase 3 smoke: migrate (optional), create noop step, enqueue, optionally burst-run."""

from __future__ import annotations

import argparse
import sys

from web.queueing import enqueue_step
from web.step_store import create_demo_job_with_noop, get_step
from web.pg import connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue a noop QC step")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Apply PostgreSQL migrations before enqueue",
    )
    parser.add_argument(
        "--run-worker",
        action="store_true",
        help="Run a burst QC worker after enqueue",
    )
    args = parser.parse_args(argv)

    if args.migrate:
        from web.migrate_pg import apply_migrations

        applied = apply_migrations()
        print("migrations:", ",".join(applied) if applied else "none")

    job_id, step_id = create_demo_job_with_noop()
    print("job_id={}".format(job_id))
    print("step_id={}".format(step_id))
    enqueue_step(step_id, "qc")
    print("enqueued qc noop")

    if args.run_worker:
        from web.rq_worker import main as worker_main

        worker_main(["qc", "--burst"])

    with connection() as conn:
        with conn.cursor() as cur:
            step = get_step(cur, step_id)
    print("final_status={}".format(step["status"] if step else "missing"))
    return 0 if step and step["status"] == "succeeded" else (0 if not args.run_worker else 1)


if __name__ == "__main__":
    sys.exit(main())
