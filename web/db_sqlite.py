"""SQLite schema and migration helpers for the Web read model."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'cancelling', 'cancelled', 'succeeded', 'partial_success', 'failed')),
    assemblers TEXT NOT NULL,
    threads INTEGER NOT NULL,
    sample TEXT NOT NULL,
    spades_kmers TEXT,
    spades_careful INTEGER NOT NULL,
    input_r1 TEXT NOT NULL,
    input_r2 TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    archive_path TEXT,
    log_path TEXT NOT NULL,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    process_group_id INTEGER,
    process_started_at TEXT,
    rerun_of TEXT,
    sample_id TEXT,
    sequencing_batch_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    completed_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TEXT,
    worker_id TEXT,
    cancel_requested_at TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_owner_sequence ON jobs(owner, sequence DESC);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, sequence);
CREATE TABLE IF NOT EXISTS job_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')
    ),
    started_at TEXT,
    completed_at TEXT,
    exit_code INTEGER,
    error TEXT,
    UNIQUE(job_id, step_name)
);
CREATE INDEX IF NOT EXISTS job_steps_job ON job_steps(job_id, id);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('r1', 'r2')),
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'uploading', 'completed', 'failed', 'consumed')
    ),
    assembled_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(owner, created_at DESC);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    job_id TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS notices_owner_id ON notices(owner, id DESC);
CREATE TABLE IF NOT EXISTS samples (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (owner, name)
);
CREATE INDEX IF NOT EXISTS samples_owner_name ON samples(owner, name);
CREATE TABLE IF NOT EXISTS sequencing_batches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    r1_filename TEXT NOT NULL,
    r2_filename TEXT NOT NULL,
    r1_sha256 TEXT,
    r2_sha256 TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS sequencing_batches_owner_sample
    ON sequencing_batches(owner, sample_id);
"""


def relocate_legacy_database(legacy: Path, database: Path) -> None:
    if database.exists() or not legacy.is_file():
        return
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(legacy) + suffix)
        if sidecar.is_file():
            relocate_file(sidecar, Path(str(database) + suffix))
    relocate_file(legacy, database)


def relocate_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(destination))


def ensure_cancellation_schema(db: sqlite3.Connection) -> None:
    columns = {info[1] for info in db.execute("PRAGMA table_info(jobs)")}
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    status_ready = bool(table_sql and table_sql[0] and "'cancelled'" in table_sql[0])
    if status_ready and {"cancel_requested", "worker_pid"} <= columns:
        return
    if status_ready:
        if "cancel_requested" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )
        if "worker_pid" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN worker_pid INTEGER")
        return
    db.execute("ALTER TABLE jobs RENAME TO jobs_legacy_status")
    db.execute(SCHEMA.split("CREATE INDEX", 1)[0].strip().rstrip(";"))
    shared = [
        info[1]
        for info in db.execute("PRAGMA table_info(jobs)")
        if info[1] in columns
    ]
    column_list = ", ".join(shared)
    db.execute(
        "INSERT INTO jobs ({}) SELECT {} FROM jobs_legacy_status".format(
            column_list, column_list
        )
    )
    db.execute("DROP TABLE jobs_legacy_status")
    db.executescript(
        "CREATE INDEX IF NOT EXISTS jobs_owner_sequence ON jobs(owner, sequence DESC);"
        "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, sequence);"
    )


def ensure_partial_success_status(db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    if row is None or (row[0] and "partial_success" in row[0]):
        return
    db.execute("ALTER TABLE jobs RENAME TO jobs_before_partial_success")
    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS jobs")
    end = SCHEMA.index("CREATE INDEX IF NOT EXISTS jobs_owner_sequence")
    db.execute(SCHEMA[start:end].replace("IF NOT EXISTS ", "").strip().rstrip(";"))
    old_columns = [
        info[1] for info in db.execute("PRAGMA table_info(jobs_before_partial_success)")
    ]
    new_columns = [info[1] for info in db.execute("PRAGMA table_info(jobs)")]
    shared = [name for name in new_columns if name in old_columns]
    column_list = ", ".join(shared)
    db.execute(
        "INSERT INTO jobs ({columns}) SELECT {columns} FROM jobs_before_partial_success".format(
            columns=column_list
        )
    )
    db.execute("DROP TABLE jobs_before_partial_success")
    db.executescript(
        "CREATE INDEX IF NOT EXISTS jobs_owner_sequence ON jobs(owner, sequence DESC);"
        "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, sequence);"
    )
    maximum = db.execute("SELECT MAX(sequence) FROM jobs").fetchone()[0] or 0
    existing = db.execute(
        "SELECT 1 FROM sqlite_sequence WHERE name = 'jobs'"
    ).fetchone()
    if existing:
        db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'jobs'", (maximum,))
    else:
        db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('jobs', ?)", (maximum,))


def ensure_rerun_column(db: sqlite3.Connection) -> None:
    columns = {info[1] for info in db.execute("PRAGMA table_info(jobs)")}
    if "rerun_of" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN rerun_of TEXT")


def ensure_lifecycle_columns(db: sqlite3.Connection) -> None:
    columns = {info[1] for info in db.execute("PRAGMA table_info(jobs)")}
    statements = {
        "attempt": "ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
        "heartbeat_at": "ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT",
        "worker_id": "ALTER TABLE jobs ADD COLUMN worker_id TEXT",
        "cancel_requested_at": "ALTER TABLE jobs ADD COLUMN cancel_requested_at TEXT",
        "archived_at": "ALTER TABLE jobs ADD COLUMN archived_at TEXT",
        "process_group_id": "ALTER TABLE jobs ADD COLUMN process_group_id INTEGER",
        "process_started_at": "ALTER TABLE jobs ADD COLUMN process_started_at TEXT",
    }
    for name, statement in statements.items():
        if name not in columns:
            db.execute(statement)


def ensure_sample_columns(db: sqlite3.Connection) -> None:
    columns = {info[1] for info in db.execute("PRAGMA table_info(jobs)")}
    if "sample_id" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN sample_id TEXT")
    if "sequencing_batch_id" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN sequencing_batch_id TEXT")

