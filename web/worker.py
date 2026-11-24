#!/usr/bin/env python3
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from web.app import create_app


@contextmanager
def connect(database: Path):
    db = sqlite3.connect(database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recover_interrupted_jobs(database: Path) -> None:
    with connect(database) as db:
        db.execute(
            "UPDATE jobs SET status = 'failed', "
            "error = 'Worker stopped while this job was running; submit a new job.', "
            "updated_at = ?, completed_at = ? WHERE status = 'running'",
            (now(), now()),
        )


def claim_next_job(database: Path):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute(
            "SELECT * FROM jobs WHERE status = 'queued' "
            "ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        if job is None:
            db.commit()
            return None
        timestamp = now()
        db.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (timestamp, timestamp, job["id"]),
        )
        db.commit()
        return job


def update_job(database: Path, job_id: str, status: str, **fields) -> None:
    fields["status"] = status
    fields["updated_at"] = now()
    fields["completed_at"] = now()
    assignments = ", ".join("{} = ?".format(key) for key in fields)
    with connect(database) as db:
        db.execute(
            "UPDATE jobs SET {} WHERE id = ?".format(assignments),
            (*fields.values(), job_id),
        )


def process_next_job(app) -> bool:
    job = claim_next_job(app.config["DATABASE"])
    if job is None:
        return False

    output_dir = Path(job["output_dir"])
    pipeline_output_dir = output_dir / "outputs"
    log_path = Path(job["log_path"])
    try:
        pipeline_output_dir.mkdir(parents=True, exist_ok=True)
        assemblers = json.loads(job["assemblers"])
        command = [
            sys.executable,
            str(app.config["PIPELINE_PATH"]),
            "--r1",
            job["input_r1"],
            "--r2",
            job["input_r2"],
            "--outdir",
            str(pipeline_output_dir),
            "--sample",
            job["sample"],
        ]
        for assembler in assemblers:
            command.extend(["--assembler", assembler])
        if "spades" in assemblers and job["spades_kmers"]:
            command.extend(["--spades-kmers", job["spades_kmers"]])
        if "spades" in assemblers and job["spades_careful"]:
            command.append("--spades-careful")

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                "Web job {}\nSTATUS: running\nCOMMAND: {}\n".format(job["id"], command)
            )
            log_file.flush()
            result = subprocess.run(
                command,
                cwd=Path(app.config["PIPELINE_PATH"]).resolve().parent,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
                env=os.environ.copy(),
            )
        partial_output_available = (
            pipeline_output_dir / "comparison.tsv"
        ).is_file()
        if result.returncode and not partial_output_available:
            raise RuntimeError("Pipeline exited with status {}".format(result.returncode))

        archive_path = app.config["DATA_ROOT"] / "downloads" / "{}.tar.gz".format(job["id"])
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(
                pipeline_output_dir,
                arcname="results",
                recursive=True,
                filter=lambda member: None if member.issym() else member,
            )
        update_job(
            app.config["DATABASE"],
            job["id"],
            "succeeded" if result.returncode == 0 else "failed",
            archive_path=str(archive_path),
            error=(
                None
                if result.returncode == 0
                else "Some selected methods failed; download available partial results and review the job log."
            ),
        )
    except Exception as error:
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("\nWEB WORKER ERROR: {}\n".format(error))
        except OSError as log_error:
            print("Could not append job error to {}: {}".format(log_path, log_error), file=sys.stderr)
        update_job(
            app.config["DATABASE"],
            job["id"],
            "failed",
            error="The analysis failed. Review the job log for details.",
        )
    return True


def main() -> int:
    app = create_app()
    lock_path = app.config["DATA_ROOT"] / "worker.lock"
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another job worker already holds the queue lock.", file=sys.stderr)
            return 1
        recover_interrupted_jobs(app.config["DATABASE"])
        print("Serial job worker started.", flush=True)
        while True:
            if not process_next_job(app):
                time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
