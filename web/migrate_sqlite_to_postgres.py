"""Copy SQLite job metadata into PostgreSQL (ADR 0015). Backup SQLite first."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from web.migrate_pg import apply_migrations
from web.pg import connection


def _ts(value):
    if value in (None, ""):
        return None
    return value


def migrate(sqlite_path: Path, *, backup_dir: Path | None = None) -> dict:
    if not sqlite_path.is_file():
        raise RuntimeError("SQLite database not found: {}".format(sqlite_path))
    if backup_dir is None:
        backup_dir = sqlite_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / "jobs.sqlite3.{}".format(stamp)
    shutil.copy2(sqlite_path, backup)

    apply_migrations()
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    counts = {}
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                for table in ("samples", "sequencing_batches", "uploads", "jobs", "notices"):
                    rows = src.execute("SELECT * FROM {}".format(table)).fetchall()
                    counts[table] = len(rows)
                    for row in rows:
                        data = dict(row)
                        if table == "jobs":
                            cur.execute(
                                """
                                INSERT INTO jobs (
                                    id, owner, module_id, status, assemblers, threads, sample,
                                    spades_kmers, spades_careful, input_r1, input_r2,
                                    output_dir, archive_path, log_path, error,
                                    cancel_requested, cancel_requested_at, rerun_of,
                                    sample_id, sequencing_batch_id,
                                    created_at, updated_at, started_at, completed_at,
                                    attempt, heartbeat_at, worker_id, archived_at
                                ) VALUES (
                                    %(id)s, %(owner)s, %(module_id)s, %(status)s, %(assemblers)s,
                                    %(threads)s, %(sample)s, %(spades_kmers)s, %(spades_careful)s,
                                    %(input_r1)s, %(input_r2)s, %(output_dir)s, %(archive_path)s,
                                    %(log_path)s, %(error)s, %(cancel_requested)s,
                                    %(cancel_requested_at)s, %(rerun_of)s, %(sample_id)s,
                                    %(sequencing_batch_id)s, %(created_at)s, %(updated_at)s,
                                    %(started_at)s, %(completed_at)s, %(attempt)s,
                                    %(heartbeat_at)s, %(worker_id)s, %(archived_at)s
                                )
                                ON CONFLICT (id) DO NOTHING
                                """,
                                {
                                    **data,
                                    "module_id": data.get("module_id") or "bacterial-assembly",
                                    "spades_careful": bool(data.get("spades_careful")),
                                    "cancel_requested": bool(data.get("cancel_requested")),
                                    "cancel_requested_at": _ts(data.get("cancel_requested_at")),
                                    "created_at": _ts(data.get("created_at")),
                                    "updated_at": _ts(data.get("updated_at")),
                                    "started_at": _ts(data.get("started_at")),
                                    "completed_at": _ts(data.get("completed_at")),
                                    "heartbeat_at": _ts(data.get("heartbeat_at")),
                                    "archived_at": _ts(data.get("archived_at")),
                                    "attempt": data.get("attempt") or 0,
                                },
                            )
                        elif table == "samples":
                            cur.execute(
                                """
                                INSERT INTO samples (id, owner, name, created_at)
                                VALUES (%(id)s, %(owner)s, %(name)s, %(created_at)s)
                                ON CONFLICT (id) DO NOTHING
                                """,
                                {**data, "created_at": _ts(data.get("created_at"))},
                            )
                        elif table == "sequencing_batches":
                            cur.execute(
                                """
                                INSERT INTO sequencing_batches (
                                    id, owner, sample_id, r1_filename, r2_filename,
                                    r1_sha256, r2_sha256, created_at
                                ) VALUES (
                                    %(id)s, %(owner)s, %(sample_id)s, %(r1_filename)s,
                                    %(r2_filename)s, %(r1_sha256)s, %(r2_sha256)s, %(created_at)s
                                )
                                ON CONFLICT (id) DO NOTHING
                                """,
                                {**data, "created_at": _ts(data.get("created_at"))},
                            )
                        elif table == "uploads":
                            cur.execute(
                                """
                                INSERT INTO uploads (
                                    id, owner, role, filename, size, chunk_size, chunk_count,
                                    sha256, status, assembled_path, created_at, updated_at
                                ) VALUES (
                                    %(id)s, %(owner)s, %(role)s, %(filename)s, %(size)s,
                                    %(chunk_size)s, %(chunk_count)s, %(sha256)s, %(status)s,
                                    %(assembled_path)s, %(created_at)s, %(updated_at)s
                                )
                                ON CONFLICT (id) DO NOTHING
                                """,
                                {
                                    **data,
                                    "created_at": _ts(data.get("created_at")),
                                    "updated_at": _ts(data.get("updated_at")),
                                },
                            )
                        elif table == "notices":
                            cur.execute(
                                """
                                INSERT INTO notices (owner, job_id, kind, message, created_at, read_at)
                                VALUES (%(owner)s, %(job_id)s, %(kind)s, %(message)s, %(created_at)s, %(read_at)s)
                                """,
                                {
                                    **data,
                                    "created_at": _ts(data.get("created_at")),
                                    "read_at": _ts(data.get("read_at")),
                                },
                            )
    finally:
        src.close()
    counts["backup"] = str(backup)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite metadata to PostgreSQL")
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=None,
        help="Path to jobs.sqlite3",
    )
    args = parser.parse_args(argv)
    sqlite_path = args.sqlite
    if sqlite_path is None:
        root = Path(__file__).resolve().parents[1]
        data = Path(__import__("os").environ.get("ASSEMBLY_DATA_ROOT", root / "deploy" / "data"))
        sqlite_path = data / "database" / "jobs.sqlite3"
    try:
        counts = migrate(sqlite_path)
    except Exception as error:  # noqa: BLE001
        print("Migration failed: {}".format(error), file=sys.stderr)
        return 1
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
