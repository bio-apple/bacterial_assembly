"""Start an RQ worker for the assembly or qc queue."""

from __future__ import annotations

import argparse
import sys

from rq import Worker

from web.queueing import ASSEMBLY_QUEUE, QC_QUEUE, redis_connection
from web.runtime_config import validate_worker_runtime, wire_tool_runtime_hooks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RQ worker for bacterial assembly")
    parser.add_argument(
        "role",
        choices=("assembly", "qc"),
        help="Worker role / queue to consume",
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="Process queued jobs then exit",
    )
    args = parser.parse_args(argv)
    validate_worker_runtime(check_docker=True)
    wire_tool_runtime_hooks()
    queue_name = ASSEMBLY_QUEUE if args.role == "assembly" else QC_QUEUE
    # Import jobs module so RQ can resolve callables.
    import web.rq_jobs  # noqa: F401

    worker = Worker(
        [queue_name],
        connection=redis_connection(),
        name="{}-{}".format(args.role, os_getpid()),
    )
    worker.work(burst=args.burst, logging_level="INFO")
    return 0


def os_getpid() -> int:
    import os

    return os.getpid()


if __name__ == "__main__":
    sys.exit(main())
