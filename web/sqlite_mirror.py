"""Mirror Postgres/RQ job progress into the SQLite Web read model."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from web.job_paths import load_job_stage_record
from web.steps import sync_job_steps
from web.storage_paths import contained_path


def sqlite_database_path() -> Path | None:
    database = os.environ.get("ASSEMBLY_SQLITE_PATH", "").strip()
    if not database:
        data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
        if data_root:
            database = str(Path(data_root) / "database" / "jobs.sqlite3")
    if not database:
        return None
    path = Path(database)
    return path if path.is_file() else None


def mirror_job_status(
    job_id: str,
    status: str,
    error: str | None = None,
    *,
    terminal: bool | None = None,
) -> None:
    path = sqlite_database_path()
    if path is None:
        return
    if terminal is None:
        terminal = status in {
            "succeeded",
            "partial_success",
            "failed",
            "cancelled",
        }
    try:
        db = sqlite3.connect(path, timeout=30)
        try:
            if terminal:
                db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = ?,
                        completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        worker_pid = NULL, process_group_id = NULL,
                        process_started_at = NULL
                    WHERE id = ?
                    """,
                    (status, error, job_id),
                )
            else:
                db.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = COALESCE(?, error),
                        started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (status, error, job_id),
                )
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        return


def mirror_job_archive(job_id: str, archive_path: str) -> None:
    path = sqlite_database_path()
    if path is None:
        return
    try:
        db = sqlite3.connect(path, timeout=30)
        try:
            db.execute(
                """
                UPDATE jobs
                SET archive_path = ?,
                    archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (archive_path, job_id),
            )
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        return


def sync_sqlite_steps_from_pg(job_id: str) -> None:
    """Project Postgres job_steps into SQLite read model (not stages.json)."""
    path = sqlite_database_path()
    if path is None:
        return
    try:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute(
                "SELECT assemblers FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            try:
                assemblers = json.loads(row["assemblers"])
            except (TypeError, json.JSONDecodeError):
                return
            if not isinstance(assemblers, list):
                return
            from web.step_read_model import recorded_steps_from_pg

            steps = recorded_steps_from_pg(job_id, assemblers)
            for step in steps:
                db.execute(
                    "INSERT INTO job_steps (job_id, step_name, status, started_at, completed_at, exit_code, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id, step_name) DO UPDATE SET "
                    "status = excluded.status, "
                    "started_at = COALESCE(excluded.started_at, job_steps.started_at), "
                    "completed_at = excluded.completed_at, "
                    "exit_code = COALESCE(excluded.exit_code, job_steps.exit_code), "
                    "error = excluded.error",
                    (
                        job_id,
                        step["name"],
                        step["status"],
                        step.get("started_at"),
                        step.get("completed_at"),
                        step.get("exit_code"),
                        step.get("error"),
                    ),
                )
            db.commit()
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        return


def sync_sqlite_steps_from_disk(job_id: str) -> None:
    """Deprecated: legacy sqlite worker only. Prefer sync_sqlite_steps_from_pg."""
    path = sqlite_database_path()
    if path is None:
        return
    try:
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute(
                "SELECT assemblers, status, output_dir FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            try:
                assemblers = json.loads(row["assemblers"])
            except (TypeError, json.JSONDecodeError):
                return
            if not isinstance(assemblers, list):
                return
            output_raw = row["output_dir"]
            data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
            try:
                if data_root:
                    output_dir = contained_path(Path(data_root), output_raw)
                else:
                    output_dir = Path(output_raw)
            except (OSError, RuntimeError, ValueError):
                output_dir = Path(output_raw)
            record = load_job_stage_record(output_dir)
            sync_job_steps(db, job_id, assemblers, record, row["status"])
            db.commit()
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        return
