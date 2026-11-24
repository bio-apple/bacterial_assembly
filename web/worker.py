#!/usr/bin/env python3
import errno
import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scripts.stage_record import load_stage_record
from web.app import append_job_log, contained_path, create_app
from web.errors import WORKER_INTERRUPTED, explain_job_failure
from web.notices import note_disk, note_job, volume_is_low


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


TERM_GRACE_SECONDS = 5.0
FINAL_CONTIGS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}
INCOMPLETE_TEMP_DIRS = {
    "spades": ("tmp", "corrected", "misc"),
    "unicycler": ("spades_output", "read_correction", "pilon_polish"),
    "shovill": ("tmp",),
    "megahit": ("tmp", "intermediate_contigs"),
}


def signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def stop_process_group(process: subprocess.Popen, grace_seconds: float = TERM_GRACE_SECONDS) -> int | None:
    if process.poll() is not None:
        return process.returncode
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        try:
            return process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return process.poll()
    signal_group(pgid, signal.SIGTERM)
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_group(pgid, signal.SIGKILL)
        try:
            return process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return process.poll()


def stop_pid_group(pid: int, grace_seconds: float = TERM_GRACE_SECONDS) -> None:
    try:
        os.kill(pid, 0)
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != pid:
        return
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    signal_group(pgid, signal.SIGKILL)


def remove_incomplete_tool_temp(pipeline_output_dir: Path) -> None:
    assemblies = pipeline_output_dir / "assemblies"
    if not assemblies.is_dir():
        return
    root = assemblies.resolve()
    for method, final_name in FINAL_CONTIGS.items():
        method_dir = assemblies / method
        if not method_dir.is_dir() or method_dir.is_symlink():
            continue
        if (method_dir / final_name).is_file():
            continue
        for name in INCOMPLETE_TEMP_DIRS[method]:
            target = method_dir / name
            if not target.is_dir() or target.is_symlink():
                continue
            resolved = target.resolve()
            if root not in resolved.parents:
                continue
            shutil.rmtree(resolved)


def cancellation_requested(database: Path, job_id: str) -> bool:
    with connect(database) as db:
        row = db.execute(
            "SELECT cancel_requested, status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelling"))


def mark_cancelling(database: Path, job_id: str) -> None:
    with connect(database) as db:
        updated = db.execute(
            "UPDATE jobs SET status = 'cancelling', updated_at = ? "
            "WHERE id = ? AND status = 'running'",
            (now(), job_id),
        )
        if updated.rowcount != 1:
            return
        row = db.execute(
            "SELECT owner, sample FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row:
            note_job(db, row["owner"], row["sample"], job_id, "cancelling")


def set_worker_pid(database: Path, job_id: str, pid: int | None) -> None:
    with connect(database) as db:
        db.execute(
            "UPDATE jobs SET worker_pid = ?, updated_at = ? WHERE id = ?",
            (pid, now(), job_id),
        )


def finalize_cancelled(database: Path, job_id: str, log_path: Path | None, pipeline_output_dir: Path | None) -> None:
    if pipeline_output_dir is not None:
        try:
            remove_incomplete_tool_temp(pipeline_output_dir)
        except OSError as error:
            if log_path is not None:
                append_job_log(log_path, "Could not remove incomplete tool temp: {}".format(error))
    with connect(database) as db:
        row = db.execute(
            "SELECT owner, sample FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        updated = db.execute(
            "UPDATE jobs SET status = 'cancelled', error = NULL, worker_pid = NULL, "
            "updated_at = ?, completed_at = ? "
            "WHERE id = ? AND status IN ('running', 'cancelling')",
            (now(), now(), job_id),
        )
        changed = updated.rowcount == 1
        if changed and row:
            note_job(db, row["owner"], row["sample"], job_id, "cancelled")
    if changed and log_path is not None:
        append_job_log(log_path, "STATUS: cancelled")


def recover_interrupted_jobs(database: Path) -> None:
    with connect(database) as db:
        rows = db.execute(
            "SELECT id, owner, sample, status, cancel_requested, worker_pid, log_path FROM jobs "
            "WHERE status IN ('running', 'cancelling')"
        ).fetchall()
    for row in rows:
        if row["worker_pid"]:
            stop_pid_group(int(row["worker_pid"]), grace_seconds=2)
    timestamp = now()
    with connect(database) as db:
        for row in rows:
            if row["status"] == "cancelling" or row["cancel_requested"]:
                updated = db.execute(
                    "UPDATE jobs SET status = 'cancelled', worker_pid = NULL, "
                    "updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status IN ('running', 'cancelling')",
                    (timestamp, timestamp, row["id"]),
                )
                if updated.rowcount == 1:
                    note_job(db, row["owner"], row["sample"], row["id"], "cancelled")
                append_job_log(row["log_path"], "STATUS: cancelled")
            else:
                updated = db.execute(
                    "UPDATE jobs SET status = 'failed', worker_pid = NULL, "
                    "error = ?, updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (WORKER_INTERRUPTED, timestamp, timestamp, row["id"]),
                )
                if updated.rowcount == 1:
                    note_job(db, row["owner"], row["sample"], row["id"], "failed")


def claim_next_job(database: Path):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        timestamp = now()
        pending = db.execute(
            "SELECT id, owner, sample, log_path FROM jobs "
            "WHERE status = 'queued' AND cancel_requested = 1"
        ).fetchall()
        for row in pending:
            updated = db.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (timestamp, timestamp, row["id"]),
            )
            if updated.rowcount == 1:
                note_job(db, row["owner"], row["sample"], row["id"], "cancelled")
            append_job_log(row["log_path"], "STATUS: cancelled")
        job = db.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND cancel_requested = 0 "
            "ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        if job is None:
            db.commit()
            return None
        updated = db.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued' AND cancel_requested = 0",
            (timestamp, timestamp, job["id"]),
        )
        if updated.rowcount == 1:
            note_job(db, job["owner"], job["sample"], job["id"], "running")
        db.commit()
        if updated.rowcount != 1:
            return None
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
        if status in ("succeeded", "failed", "cancelled"):
            row = db.execute(
                "SELECT owner, sample FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row:
                note_job(db, row["owner"], row["sample"], job_id, status)


def publish_directory(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = destination / child.name
        try:
            child.replace(target)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            shutil.move(str(child), str(target))


def remember_low_disk(app, job) -> None:
    try:
        low = volume_is_low(app.config["DATA_ROOT"])
    except OSError:
        return
    if not low:
        return
    with connect(app.config["DATABASE"]) as db:
        note_disk(db, job["owner"], job["sample"], job["id"])


def _log_text(log_path: Path | None) -> str:
    if log_path is None or not log_path.is_file():
        return ""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _stages(pipeline_output_dir: Path | None):
    if pipeline_output_dir is None:
        return []
    return load_stage_record(pipeline_output_dir / "stages.json").get("stages") or []


def failure_message(returncode, error, log_path, pipeline_output_dir, partial: bool) -> str:
    _code, message = explain_job_failure(
        returncode,
        error,
        _log_text(log_path),
        _stages(pipeline_output_dir),
        partial,
    )
    return message


def process_next_job(app) -> bool:
    job = claim_next_job(app.config["DATABASE"])
    if job is None:
        return False

    data_root = app.config["DATA_ROOT"]
    log_path = None
    pipeline_output_dir = None
    cancelled = False
    returncode = None
    remember_low_disk(app, job)
    try:
        output_dir = contained_path(data_root, job["output_dir"])
        log_path = contained_path(data_root, job["log_path"])
        input_r1 = contained_path(data_root, job["input_r1"])
        input_r2 = contained_path(data_root, job["input_r2"])
        job_id = str(uuid.UUID(str(job["id"])))
        work_dir = output_dir.parent / "work"
        publish = output_dir.name == "output" and work_dir.is_dir()
        pipeline_output_dir = work_dir if publish else output_dir / "outputs"
        if not publish:
            pipeline_output_dir.mkdir(parents=True, exist_ok=True)
        assemblers = json.loads(job["assemblers"])
        command = [
            sys.executable,
            str(app.config["PIPELINE_PATH"]),
            "--r1",
            str(input_r1),
            "--r2",
            str(input_r2),
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

        returncode = None
        with log_path.open("a", encoding="utf-8") as log_file:
            if cancellation_requested(app.config["DATABASE"], job["id"]):
                mark_cancelling(app.config["DATABASE"], job["id"])
                log_file.write("STATUS: cancelling\n")
                log_file.flush()
                cancelled = True
            else:
                log_file.write(
                    "Web job {}\nSTATUS: running\nCOMMAND: {}\n".format(job["id"], command)
                )
                log_file.flush()
                process = subprocess.Popen(
                    command,
                    cwd=Path(app.config["PIPELINE_PATH"]).resolve().parent,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    start_new_session=True,
                )
                set_worker_pid(app.config["DATABASE"], job["id"], process.pid)
                while process.poll() is None:
                    if cancellation_requested(app.config["DATABASE"], job["id"]):
                        mark_cancelling(app.config["DATABASE"], job["id"])
                        log_file.write("STATUS: cancelling\n")
                        log_file.flush()
                        stop_process_group(process)
                        cancelled = True
                        break
                    time.sleep(0.2)
                returncode = process.returncode
        if cancelled or (
            returncode not in (0, None) and cancellation_requested(app.config["DATABASE"], job["id"])
        ):
            if not cancelled:
                mark_cancelling(app.config["DATABASE"], job["id"])
            finalize_cancelled(
                app.config["DATABASE"], job["id"], log_path, pipeline_output_dir
            )
            return True

        partial_output_available = (
            pipeline_output_dir / "comparison.tsv"
        ).is_file()
        if returncode and not partial_output_available:
            raise RuntimeError("Pipeline exited with status {}".format(returncode))

        archived_dir = pipeline_output_dir
        if publish:
            publish_directory(pipeline_output_dir, output_dir)
            archived_dir = output_dir
        archive_path = contained_path(
            data_root, app.config["ARCHIVE_ROOT"] / "{}.tar.gz".format(job_id)
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(
                archived_dir,
                arcname="results",
                recursive=True,
                filter=lambda member: None if member.issym() else member,
            )
        update_job(
            app.config["DATABASE"],
            job["id"],
            "succeeded" if returncode == 0 else "failed",
            archive_path=str(archive_path),
            worker_pid=None,
            error=(
                None
                if returncode == 0
                else failure_message(returncode, None, log_path, pipeline_output_dir, True)
            ),
        )
    except Exception as error:
        if cancelled or cancellation_requested(app.config["DATABASE"], job["id"]):
            finalize_cancelled(
                app.config["DATABASE"], job["id"], log_path, pipeline_output_dir
            )
            return True
        try:
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write("\nWEB WORKER ERROR:\n{}\n".format(traceback.format_exc()))
        except OSError as log_error:
            print("Could not append job error to {}: {}".format(log_path, log_error), file=sys.stderr)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            try:
                with connect(app.config["DATABASE"]) as db:
                    note_disk(db, job["owner"], job["sample"], job["id"])
            except sqlite3.Error:
                print("Could not record disk notice for {}".format(job["id"]), file=sys.stderr)
        update_job(
            app.config["DATABASE"],
            job["id"],
            "failed",
            worker_pid=None,
            error=failure_message(returncode, error, log_path, pipeline_output_dir, False),
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
