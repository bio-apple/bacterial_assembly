"""Plan steps, enqueue dependents, finalize jobs for the RQ pipeline."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

from web.pg import connection
from web.queueing import enqueue_step
from web.step_runners import ASSEMBLY_CONTIGS, work_dir_for_job
from web.step_store import new_id, utc_now


def plan_and_insert_job(
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
) -> list[str]:
    """Insert PG job + pending steps. Returns step ids in creation order."""
    step_ids: list[str] = []
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id, owner, status, assemblers, threads, sample,
                    spades_kmers, spades_careful, input_r1, input_r2,
                    output_dir, log_path
                ) VALUES (
                    %s, %s, 'queued', %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    job_id,
                    owner,
                    json.dumps(assemblers),
                    threads,
                    sample,
                    spades_kmers,
                    spades_careful,
                    input_r1,
                    input_r2,
                    output_dir,
                    log_path,
                ),
            )
            fastp_id = new_id()
            cur.execute(
                """
                INSERT INTO job_steps (
                    id, job_id, step_name, step_type, software, status, worker_type
                ) VALUES (%s, %s, 'fastp', 'fastp', 'fastp', 'pending', 'qc')
                """,
                (fastp_id, job_id),
            )
            step_ids.append(fastp_id)
            for method in assemblers:
                sid = new_id()
                cur.execute(
                    """
                    INSERT INTO job_steps (
                        id, job_id, step_name, step_type, software, status, worker_type
                    ) VALUES (%s, %s, %s, 'assembly', %s, 'pending', 'assembly')
                    """,
                    (sid, job_id, method, method),
                )
                step_ids.append(sid)
                qid = new_id()
                cur.execute(
                    """
                    INSERT INTO job_steps (
                        id, job_id, step_name, step_type, software, status, worker_type
                    ) VALUES (%s, %s, %s, 'quast', %s, 'pending', 'qc')
                    """,
                    (qid, job_id, "quast:{}".format(method), method),
                )
                step_ids.append(qid)
            summary_id = new_id()
            cur.execute(
                """
                INSERT INTO job_steps (
                    id, job_id, step_name, step_type, software, status, worker_type
                ) VALUES (%s, %s, 'summary', 'summary', 'summary', 'pending', 'qc')
                """,
                (summary_id, job_id),
            )
            step_ids.append(summary_id)
    return step_ids


def start_job(job_id: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM job_steps WHERE job_id = %s AND step_name = 'fastp'",
                (job_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("fastp step missing for {}".format(job_id))
    enqueue_step(str(row["id"]), "qc")


def _steps_by_name(cur, job_id: str) -> dict[str, dict]:
    cur.execute("SELECT * FROM job_steps WHERE job_id = %s", (job_id,))
    return {row["step_name"]: row for row in cur.fetchall()}


def on_step_finished(step_id: str, terminal_status: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job_steps WHERE id = %s", (step_id,))
            step = cur.fetchone()
            if step is None:
                return
            job_id = str(step["job_id"])
            cur.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
            job = cur.fetchone()
            if job is None:
                return
            if job["status"] in {"cancelling", "cancelled"}:
                return
            steps = _steps_by_name(cur, job_id)

    step_name = step["step_name"]
    step_type = step["step_type"]

    if step_type == "fastp":
        if terminal_status != "succeeded":
            _fail_job(job_id, "fastp failed")
            return
        assemblers = json.loads(job["assemblers"])
        for method in assemblers:
            enqueue_step(str(steps[method]["id"]), "assembly")
        return

    if step_type == "assembly":
        method = step["software"] or step_name
        quast_name = "quast:{}".format(method)
        if terminal_status == "succeeded":
            enqueue_step(str(steps[quast_name]["id"]), "qc")
        else:
            _skip_step(str(steps[quast_name]["id"]), "assembly failed")
        _maybe_enqueue_summary(job_id)
        return

    if step_type == "quast":
        _maybe_enqueue_summary(job_id)
        return

    if step_type == "summary":
        _finalize_job(job_id)
        return

    if step_type == "noop":
        return


def _maybe_enqueue_summary(job_id: str) -> None:
    if not (_assemblies_terminal(job_id) and _quasts_terminal(job_id)):
        return
    with connection() as conn:
        with conn.cursor() as cur:
            steps = _steps_by_name(cur, job_id)
            summary = steps.get("summary")
            if summary and summary["status"] in {"pending"}:
                enqueue_step(str(summary["id"]), "qc")


def _assemblies_terminal(job_id: str) -> bool:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM job_steps
                WHERE job_id = %s AND step_type = 'assembly'
                """,
                (job_id,),
            )
            rows = cur.fetchall()
    return bool(rows) and all(
        row["status"] in {"succeeded", "failed", "cancelled", "skipped"} for row in rows
    )


def _quasts_terminal(job_id: str) -> bool:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status FROM job_steps
                WHERE job_id = %s AND step_type = 'quast'
                """,
                (job_id,),
            )
            rows = cur.fetchall()
    return all(
        row["status"] in {"succeeded", "failed", "cancelled", "skipped"} for row in rows
    )


def _skip_step(step_id: str, reason: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'skipped', error = %s, completed_at = %s, updated_at = %s
                WHERE id = %s AND status IN ('pending', 'queued')
                """,
                (reason, utc_now(), utc_now(), step_id),
            )


def _fail_job(job_id: str, error: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = %s, completed_at = %s, updated_at = %s
                WHERE id = %s
                """,
                (error, utc_now(), utc_now(), job_id),
            )
            cur.execute(
                """
                UPDATE job_steps
                SET status = 'skipped', error = %s, completed_at = %s, updated_at = %s
                WHERE job_id = %s AND status IN ('pending', 'queued')
                """,
                (error, utc_now(), utc_now(), job_id),
            )
    _mirror_sqlite_status(job_id, "failed", error)
    _publish_and_archive(job_id)


def _finalize_job(job_id: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
            cur.execute(
                """
                SELECT step_name, status FROM job_steps
                WHERE job_id = %s AND step_type = 'assembly'
                """,
                (job_id,),
            )
            assemblies = cur.fetchall()
    if job is None:
        return
    selected = json.loads(job["assemblers"])
    successful = [row["step_name"] for row in assemblies if row["status"] == "succeeded"]
    failed = [row["step_name"] for row in assemblies if row["status"] != "succeeded"]
    if not successful:
        status = "failed"
    elif failed:
        status = "partial_success"
    else:
        status = "succeeded"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = %s, completed_at = %s, updated_at = %s, error = NULL
                WHERE id = %s
                """,
                (status, utc_now(), utc_now(), job_id),
            )
    _mirror_sqlite_status(job_id, status, None)
    _publish_and_archive(job_id)


def _mirror_sqlite_status(job_id: str, status: str, error: str | None) -> None:
    database = os.environ.get("ASSEMBLY_SQLITE_PATH", "").strip()
    if not database:
        data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
        if data_root:
            database = str(Path(data_root) / "database" / "jobs.sqlite3")
    if not database or not Path(database).is_file():
        return
    try:
        db = sqlite3.connect(database, timeout=30)
        try:
            db.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    worker_pid = NULL, process_group_id = NULL, process_started_at = NULL
                WHERE id = ?
                """,
                (status, error, job_id),
            )
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        return


def _publish_and_archive(job_id: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
    if job is None:
        return
    output_dir = Path(job["output_dir"])
    work = work_dir_for_job(job)
    if work.is_dir() and output_dir.name == "output":
        _publish_directory(work, output_dir)
        archived_dir = output_dir
    else:
        archived_dir = work if work.is_dir() else output_dir
    data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
    if not data_root:
        return
    archive_path = Path(data_root) / "archive" / "{}.tar.gz".format(job_id)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial = archive_path.with_name(archive_path.name + ".partial")
    try:
        partial.unlink(missing_ok=True)
        with tarfile.open(partial, "w:gz") as archive:
            archive.add(
                archived_dir,
                arcname="results",
                recursive=True,
                filter=lambda member: None if member.issym() else member,
            )
        if partial.is_file() and partial.stat().st_size > 0:
            partial.replace(archive_path)
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET archive_path = %s, archived_at = %s WHERE id = %s",
                        (str(archive_path), utc_now(), job_id),
                    )
            _mirror_sqlite_archive(job_id, str(archive_path))
    except OSError:
        partial.unlink(missing_ok=True)


def _mirror_sqlite_archive(job_id: str, archive_path: str) -> None:
    database = os.environ.get("ASSEMBLY_SQLITE_PATH", "").strip()
    if not database:
        data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
        if data_root:
            database = str(Path(data_root) / "database" / "jobs.sqlite3")
    if not database or not Path(database).is_file():
        return
    try:
        db = sqlite3.connect(database, timeout=30)
        try:
            db.execute(
                "UPDATE jobs SET archive_path = ?, archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (archive_path, job_id),
            )
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        return


def _publish_directory(source: Path, destination: Path) -> None:
    incoming = destination.with_name(destination.name + ".incoming")
    if incoming.exists():
        shutil.rmtree(incoming)
    shutil.copytree(source, incoming)
    if destination.exists():
        shutil.rmtree(destination)
    incoming.rename(destination)
    # Clear work dir contents but keep directory for path stability.
    for child in source.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


# Silence unused import lint for contig map documentation.
_ = ASSEMBLY_CONTIGS
