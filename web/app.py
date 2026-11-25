#!/usr/bin/env python3
import csv
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path, PurePosixPath

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from scripts.common import cpu_share
from scripts.stage_record import build_progress, load_stage_record
from web.reports import (
    assemble_report_model,
    comparison_view,
    report_tsv,
    resource_lines,
    result_sources_present,
    selected_assemblers,
)
from web.errors import (
    DISK_FULL,
    FASTQ_CHECK_UNAVAILABLE,
    FASTQ_EMPTY,
    FASTQ_INVALID,
    FASTQ_RECORDS,
    INTERNAL,
    PARAMETER_KMER,
    PARAMETER_THREADS,
    RESOURCE_START,
    UPLOAD_FAILED,
    parameter_message,
)
from web.fastq_check import FastqCheckUnavailable, FastqRecordError, check_fastq_records
from web.host_monitor import format_bytes, snapshot
from web.modules import ANALYSIS_MODULES, BACTERIAL_ASSEMBLY_MODULE_ID, group_enabled_modules
from web.notices import note_disk, note_job

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
JOB_STATUSES = ("queued", "running", "cancelling", "cancelled", "succeeded", "failed")
FINISHED_STATUSES = ("succeeded", "failed", "cancelled")
JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "分析中",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "succeeded": "已完成",
    "failed": "失败",
}
RESULT_FILENAMES = {"comparison.tsv"}
RESULT_SUFFIXES = (".fasta", ".fa", ".fna", ".fas")
FASTQ_SUFFIXES = (".fastq", ".fq", ".fastq.gz", ".fq.gz")
PATH_FIELDS = {
    "path",
    "dest",
    "directory",
    "staging_path",
    "output_dir",
    "assembled_path",
    "input_r1",
    "input_r2",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'cancelling', 'cancelled', 'succeeded', 'failed')),
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


def load_users(path: Path) -> dict[str, str]:
    try:
        contents = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Cannot load configured users file: {}".format(path)) from error
    users = contents.get("users") if isinstance(contents, dict) else None
    if not isinstance(users, dict) or not users:
        raise RuntimeError("Users file must contain a non-empty 'users' object")
    for username, password_hash in users.items():
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username)
            or not isinstance(password_hash, str)
            or not password_hash.startswith(("scrypt:", "pbkdf2:"))
        ):
            raise RuntimeError("Users file contains an invalid username or password hash")
    return users


def configure_storage(app: Flask) -> None:
    root = Path(app.config["DATA_ROOT"]).resolve()
    app.config["DATA_ROOT"] = root
    root.mkdir(parents=True, exist_ok=True)
    directories = {
        "UPLOAD_INCOMING": root / "uploads" / "incoming",
        "UPLOAD_CHUNKS": root / "uploads" / "chunks",
        "UPLOAD_VALIDATED": root / "uploads" / "validated",
        "JOBS_ROOT": root / "jobs",
        "REPORTS_ROOT": root / "reports",
        "DATABASE_DIR": root / "database",
        "TEMP_ROOT": root / "temp",
        "ARCHIVE_ROOT": root / "archive",
    }
    for key, path in directories.items():
        path.mkdir(parents=True, exist_ok=True)
        app.config[key] = path
    # Chunked uploads already stage bytes under STAGING_ROOT. That path is chunks.
    app.config["STAGING_ROOT"] = app.config["UPLOAD_CHUNKS"]
    database = app.config["DATABASE_DIR"] / "jobs.sqlite3"
    relocate_legacy_database(root / "jobs.sqlite3", database)
    app.config["DATABASE"] = database
    temp_root = str(app.config["TEMP_ROOT"])
    os.environ["TMPDIR"] = temp_root
    os.environ["TMP"] = temp_root
    os.environ["TEMP"] = temp_root
    tempfile.tempdir = temp_root


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


def contained_path(root: Path, raw: str | Path) -> Path:
    root_resolved = root.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("path is outside the data root")
    return resolved


def data_file(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        path = contained_path(current_app.config["DATA_ROOT"], raw)
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


def stage_record_path(output_dir: Path) -> Path:
    candidates = (
        output_dir.parent / "work" / "stages.json",
        output_dir / "stages.json",
        output_dir / "outputs" / "stages.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def discard_stored_files(files: list[Path], directories: list[Path]) -> None:
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


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


def reuse_or_create_sample(db: sqlite3.Connection, owner: str, name: str) -> str:
    existing = db.execute(
        "SELECT id FROM samples WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()
    if existing:
        return existing[0]
    sample_id = str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO samples (id, owner, name) VALUES (?, ?, ?)",
            (sample_id, owner, name),
        )
    except sqlite3.IntegrityError:
        existing = db.execute(
            "SELECT id FROM samples WHERE owner = ? AND name = ?",
            (owner, name),
        ).fetchone()
        if existing is None:
            raise
        return existing[0]
    return sample_id


def checksum_or_none(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def column_value(row: sqlite3.Row, name: str):
    return row[name] if name in row.keys() else None


def append_job_log(log_path: str | Path, text: str) -> None:
    path = Path(log_path)
    line = text if text.endswith("\n") else text + "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("ASSEMBLY_WEB_SECRET"),
        USERS_FILE=os.environ.get("ASSEMBLY_USERS_FILE", "/config/users.json"),
        DATA_ROOT=os.environ.get("ASSEMBLY_DATA_ROOT", "/data"),
        PIPELINE_PATH=Path(__file__).resolve().parents[1] / "run_pipeline.py",
        ANALYSIS_MODULES=ANALYSIS_MODULES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        MAX_CONTENT_LENGTH=None,
        UPLOAD_CHUNK_SIZE=8 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not isinstance(app.config["SECRET_KEY"], str) or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("Set ASSEMBLY_WEB_SECRET to a random secret of at least 32 characters")
    app.config["USERS"] = app.config.get("USERS") or load_users(
        Path(app.config["USERS_FILE"])
    )
    app.config["UPLOAD_CHUNK_SIZE"] = int(app.config["UPLOAD_CHUNK_SIZE"])
    if app.config["UPLOAD_CHUNK_SIZE"] < 1:
        raise RuntimeError("UPLOAD_CHUNK_SIZE must be a positive integer")
    configure_storage(app)
    db = sqlite3.connect(app.config["DATABASE"])
    try:
        db.executescript(SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "memory" in columns or "module_id" not in columns:
            db.execute("ALTER TABLE jobs RENAME TO jobs_legacy_resources")
            db.execute(
                SCHEMA.split("CREATE INDEX", 1)[0].strip().rstrip(";")
            )
            target_columns = (
                "sequence, id, owner, module_id, status, assemblers, threads, sample, "
                "spades_kmers, spades_careful, input_r1, input_r2, output_dir, "
                "archive_path, log_path, error, created_at, updated_at, started_at, "
                "completed_at"
            )
            source_module = (
                "module_id" if "module_id" in columns else "'bacterial-assembly'"
            )
            source_columns = (
                f"sequence, id, owner, {source_module}, status, assemblers, threads, "
                "sample, spades_kmers, spades_careful, input_r1, input_r2, output_dir, "
                "archive_path, log_path, error, created_at, updated_at, started_at, "
                "completed_at"
            )
            db.execute(
                "INSERT INTO jobs ({}) SELECT {} FROM jobs_legacy_resources".format(
                    target_columns, source_columns
                )
            )
            db.execute("DROP TABLE jobs_legacy_resources")
            db.executescript(
                "CREATE INDEX IF NOT EXISTS jobs_owner_sequence "
                "ON jobs(owner, sequence DESC);"
                "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, sequence);"
            )
        ensure_cancellation_schema(db)
        ensure_rerun_column(db)
        ensure_sample_columns(db)
        ensure_lifecycle_columns(db)
        db.commit()
    finally:
        db.close()

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'; object-src 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def analysis_module_context():
        modules = app.config["ANALYSIS_MODULES"]
        active_module_id = next(
            (module["id"] for module in modules if module["endpoint"] == request.endpoint),
            None,
        )
        return {
            "analysis_modules": modules,
            "active_module_id": active_module_id,
            "active_page": {
                "overview": "overview",
                "job_history": "history",
                "compare_jobs": "history",
                "module_index": "modules",
                "job_page": "monitor",
                "job_report": "monitor",
            }.get(request.endpoint, "modules"),
        }

    @contextmanager
    def connection():
        db = sqlite3.connect(app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def fail(code: str, message: str, status: int):
        return jsonify(code=code, message=message, error=message), status

    def storage_error(error: OSError, log_message: str):
        app.logger.exception(log_message)
        if error.errno == errno.ENOSPC:
            owner = session.get("username")
            if owner:
                try:
                    with connection() as db:
                        note_disk(db, owner)
                except sqlite3.Error:
                    app.logger.exception("Could not record disk notice")
            return fail("disk_full", DISK_FULL, 507)
        return fail("upload_failed", UPLOAD_FAILED, 507)

    def upload_value_error(error: ValueError):
        text = str(error)
        if "不能开始分析" in text:
            return fail("upload_incomplete", text, 400)
        if "分片" in text:
            return fail("upload_failed", "文件上传错误。{}".format(text), 400)
        if text == FASTQ_RECORDS:
            return fail("fastq_invalid", FASTQ_RECORDS, 400)
        if "empty" in text.lower() or "FASTQ" in text:
            return fail("fastq_invalid", FASTQ_EMPTY, 400)
        return fail("upload_invalid", text, 400)

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def require_csrf() -> None:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(400, description="Invalid CSRF token")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("username"):
                if request.path.startswith("/api/"):
                    return fail("authentication_required", "需要登录。", 401)
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def owned_job(job_id: str) -> sqlite3.Row:
        try:
            uuid.UUID(job_id)
        except ValueError:
            abort(404)
        with connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id = ? AND owner = ?",
                (job_id, session["username"]),
            ).fetchone()
        if row is None:
            abort(404)
        return row

    def linked_sample(row: sqlite3.Row) -> tuple[str, dict | None]:
        sample_name = row["sample"]
        sample_id = column_value(row, "sample_id")
        batch_id = column_value(row, "sequencing_batch_id")
        batch = None
        with connection() as db:
            if sample_id:
                found = db.execute(
                    "SELECT name FROM samples WHERE id = ? AND owner = ?",
                    (sample_id, row["owner"]),
                ).fetchone()
                if found:
                    sample_name = found["name"]
            if sample_id and batch_id:
                found_batch = db.execute(
                    "SELECT r1_filename, r2_filename, r1_sha256, r2_sha256 "
                    "FROM sequencing_batches WHERE id = ? AND owner = ? AND sample_id = ?",
                    (batch_id, row["owner"], sample_id),
                ).fetchone()
                if found_batch:
                    batch = {
                        "r1_filename": found_batch["r1_filename"],
                        "r2_filename": found_batch["r2_filename"],
                        "r1_sha256": found_batch["r1_sha256"],
                        "r2_sha256": found_batch["r2_sha256"],
                    }
        return sample_name, batch

    def job_detail(row: sqlite3.Row) -> dict:
        sample_name, batch = linked_sample(row)
        directories = pipeline_directories(row["output_dir"])
        run_log = first_pipeline_text(directories, "run.log", 2 * 1024 * 1024)
        resources = resource_lines(row["threads"], run_log)
        assemblers = selected_assemblers(row["assemblers"])
        comparison_text = first_pipeline_text(directories, "comparison.tsv", 2 * 1024 * 1024)
        parsed = parse_comparison_text(comparison_text)
        return {
            "sample_name": sample_name,
            "sample_id": column_value(row, "sample_id"),
            "sequencing_batch_id": column_value(row, "sequencing_batch_id"),
            "batch": batch,
            "inputs": input_facts(row),
            "parameters": {
                "assemblers": assemblers or [],
                "spades_kmers": row["spades_kmers"],
                "spades_careful": bool(row["spades_careful"]),
            },
            "resources": resources,
            "comparison": comparison_view(assemblers, parsed),
            "report_ready": result_sources_present(
                first_pipeline_text(directories, "versions.txt", 1024 * 1024),
                load_fastp_documents(directories),
                parsed,
                stage_failures(row),
            ),
            "inputs_available": inputs_ready(row),
        }

    def job_report_model(row: sqlite3.Row) -> dict | None:
        sample_name, batch = linked_sample(row)
        directories = pipeline_directories(row["output_dir"])
        versions_text = first_pipeline_text(directories, "versions.txt", 1024 * 1024)
        fastp_documents = load_fastp_documents(directories)
        comparison_rows = parse_comparison_text(
            first_pipeline_text(directories, "comparison.tsv", 2 * 1024 * 1024)
        )
        failures = stage_failures(row)
        if not result_sources_present(
            versions_text, fastp_documents, comparison_rows, failures
        ):
            return None
        assemblers = selected_assemblers(row["assemblers"])
        resources = resource_lines(
            row["threads"],
            first_pipeline_text(directories, "run.log", 2 * 1024 * 1024),
        )
        return assemble_report_model(
            sample_name=sample_name,
            status_label=JOB_STATUS_LABELS.get(row["status"], row["status"]),
            batch=batch,
            inputs=input_facts(row),
            assemblers=assemblers,
            spades_kmers=row["spades_kmers"],
            spades_careful=bool(row["spades_careful"]),
            resources=resources,
            versions_text=versions_text,
            fastp_documents=fastp_documents,
            comparison_rows=comparison_rows,
            job_error=row["error"],
            stage_failures=failures,
        )

    @app.get("/login")
    def login():
        if session.get("username"):
            return redirect(url_for("overview"))
        return render_template("login.html", csrf_token=csrf_token(), error=None)

    @app.post("/login")
    def login_post():
        require_csrf()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        password_hash = app.config["USERS"].get(username)
        valid = False
        if password_hash:
            try:
                valid = check_password_hash(password_hash, password)
            except ValueError:
                valid = False
        if not valid:
            time.sleep(0.25)
            return (
                render_template(
                    "login.html",
                    csrf_token=csrf_token(),
                    error="账号或密码不正确。",
                ),
                401,
            )
        session.clear()
        session["username"] = username
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        return redirect(url_for("overview"))

    @app.post("/logout")
    @login_required
    def logout():
        require_csrf()
        session.clear()
        return redirect(url_for("login"))

    def overview_jobs(owner: str) -> dict:
        modules = {module["id"]: module for module in app.config["ANALYSIS_MODULES"]}
        with connection() as db:
            count_rows = db.execute(
                "SELECT status, COUNT(*) AS total FROM jobs WHERE owner = ? GROUP BY status",
                (owner,),
            ).fetchall()
            recent_rows = db.execute(
                "SELECT * FROM jobs WHERE owner = ? ORDER BY sequence DESC LIMIT 6",
                (owner,),
            ).fetchall()
            report_rows = db.execute(
                "SELECT * FROM jobs WHERE owner = ? AND archive_path IS NOT NULL "
                "ORDER BY sequence DESC LIMIT 5",
                (owner,),
            ).fetchall()
        counts = {
            status: 0
            for status in (
                "queued",
                "running",
                "cancelling",
                "cancelled",
                "succeeded",
                "failed",
            )
        }
        for row in count_rows:
            if row["status"] in counts:
                counts[row["status"]] = row["total"]

        def present(row: sqlite3.Row) -> dict:
            job = serialize_job(row)
            module = modules.get(job["module_id"], {})
            job["module_name"] = module.get("name", job["module_id"])
            job["module_code"] = module.get("code", "—")
            return job

        reports = []
        for row in report_rows:
            job = present(row)
            if job["archive_available"]:
                reports.append(job)
        return {
            "counts": counts,
            "active_count": counts["queued"] + counts["running"] + counts["cancelling"],
            "total_count": sum(counts.values()),
            "recent_jobs": [present(row) for row in recent_rows],
            "reports": reports,
        }

    @app.get("/overview")
    @login_required
    def overview():
        return render_template(
            "overview.html",
            username=session["username"],
            csrf_token=csrf_token(),
            modules=app.config["ANALYSIS_MODULES"],
            host=host_view(app.config["DATA_ROOT"]),
            **overview_jobs(session["username"]),
        )

    @app.get("/api/host")
    @login_required
    def host_status():
        return jsonify(host=host_view(app.config["DATA_ROOT"]))

    def module_usage(owner: str) -> dict:
        with connection() as db:
            rows = db.execute(
                "SELECT module_id, "
                "SUM(status = 'queued') AS queued, "
                "SUM(status = 'running') AS running, "
                "SUM(status = 'cancelling') AS cancelling, "
                "MAX(created_at) AS last_used_at "
                "FROM jobs WHERE owner = ? GROUP BY module_id",
                (owner,),
            ).fetchall()
        return {
            row["module_id"]: {
                "queued": row["queued"],
                "running": row["running"],
                "cancelling": row["cancelling"],
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        }

    @app.get("/modules")
    @login_required
    def module_index():
        modules = app.config["ANALYSIS_MODULES"]
        return render_template(
            "modules.html",
            username=session["username"],
            csrf_token=csrf_token(),
            modules=modules,
            categories=group_enabled_modules(
                modules, module_usage(session["username"])
            ),
        )

    def owned_sample_names(owner: str) -> list[str]:
        with connection() as db:
            rows = db.execute(
                "SELECT name FROM samples WHERE owner = ? ORDER BY name",
                (owner,),
            ).fetchall()
        return [row["name"] for row in rows]

    @app.get("/")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            username=session["username"],
            csrf_token=csrf_token(),
            assemblers=ASSEMBLERS,
            samples=owned_sample_names(session["username"]),
        )

    def history_filters() -> dict:
        sample = request.args.get("sample", "")
        if "\x00" in sample:
            abort(400, description="样本名称不正确。")
        module_id = request.args.get("module", "")
        known_modules = {module["id"] for module in app.config["ANALYSIS_MODULES"]}
        if module_id and module_id not in known_modules:
            abort(400, description="没有这个分析模块。")
        status = request.args.get("status", "")
        if status and status not in JOB_STATUSES:
            abort(400, description="没有这个任务状态。")
        start = parse_filter_date(request.args.get("start", ""))
        end = parse_filter_date(request.args.get("end", ""))
        if start and end and start > end:
            abort(400, description="日期范围不正确。")
        return {
            "sample": sample,
            "module": module_id,
            "status": status,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "start_date": start,
            "end_date": end,
        }

    def present_history_job(row, modules, current_time) -> dict:
        job = serialize_job(row)
        module = modules.get(job["module_id"], {})
        job["module_name"] = module.get("name", job["module_id"])
        job["module_code"] = module.get("code", "—")
        job["log_available"] = data_file(row["log_path"]) is not None
        job["result_available"] = data_file(row["archive_path"]) is not None
        job["inputs_available"] = inputs_ready(row)
        job["result_files"] = notable_output_files(row["output_dir"])
        job["runtime_seconds"] = elapsed_seconds(
            job["started_at"], job["completed_at"], current_time
        )
        job["runtime_label"] = (
            "等待开始" if job["runtime_seconds"] is None else format_duration(job["runtime_seconds"])
        )
        return job

    @app.get("/history")
    @login_required
    def job_history():
        page = parse_positive_int(request.args.get("page", "1"))
        if page is None:
            abort(400, description="page must be a positive integer")
        filters = history_filters()
        page_size = 50
        owner = session["username"]
        clauses = ["owner = ?"]
        params: list = [owner]
        if filters["sample"]:
            clauses.append("instr(sample, ?) > 0")
            params.append(filters["sample"])
        if filters["module"]:
            clauses.append("module_id = ?")
            params.append(filters["module"])
        if filters["status"]:
            clauses.append("status = ?")
            params.append(filters["status"])
        if filters["start_date"]:
            clauses.append("created_at >= ?")
            params.append(filters["start"])
        if filters["end_date"]:
            clauses.append("created_at < ?")
            params.append((filters["end_date"] + timedelta(days=1)).isoformat())
        where = " AND ".join(clauses)
        with connection() as db:
            total_count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE {}".format(where), tuple(params)
            ).fetchone()[0]
            total_pages = max(1, (total_count + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = db.execute(
                "SELECT * FROM jobs WHERE {} ORDER BY sequence DESC LIMIT ? OFFSET ?".format(where),
                tuple(params + [page_size, (page - 1) * page_size]),
            ).fetchall()
            comparable_rows = db.execute(
                "SELECT id, sample, status, output_dir, created_at FROM jobs "
                "WHERE owner = ? AND status IN ('succeeded', 'failed', 'cancelled') "
                "ORDER BY sequence DESC",
                (owner,),
            ).fetchall()

        modules = {module["id"]: module for module in app.config["ANALYSIS_MODULES"]}
        current_time = datetime.now(timezone.utc)
        history_jobs = [present_history_job(row, modules, current_time) for row in rows]
        comparable_jobs = [
            {
                "id": row["id"],
                "sample": row["sample"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in comparable_rows
            if comparison_file(row["output_dir"]) is not None
        ]
        filtered = any(filters[key] for key in ("sample", "module", "status", "start", "end"))
        view_filters = {key: filters[key] for key in ("sample", "module", "status", "start", "end")}
        return render_template(
            "history.html",
            username=session["username"],
            csrf_token=csrf_token(),
            jobs=history_jobs,
            page=page,
            total_pages=total_pages,
            total_count=total_count,
            filters=view_filters,
            filtered=filtered,
            status_choices=JOB_STATUSES,
            status_labels=JOB_STATUS_LABELS,
            comparable_jobs=comparable_jobs,
            messages=get_flashed_messages(),
        )

    @app.get("/history/compare")
    @login_required
    def compare_jobs():
        left_id = request.args.get("left", "")
        right_id = request.args.get("right", "")
        if not left_id or not right_id or left_id == right_id:
            abort(400, description="请选择两个不同的任务。")
        left = owned_job(left_id)
        right = owned_job(right_id)
        summaries = []
        for row in (left, right):
            if row["status"] not in FINISHED_STATUSES:
                abort(400, description="只有已结束的任务可以比较。")
            summary_path = comparison_file(row["output_dir"])
            summary = parse_comparison_tsv(summary_path) if summary_path else []
            if not summary:
                abort(400, description="这条任务没有比较表，不能比较。")
            summaries.append(summary)
        return render_template(
            "compare.html",
            username=session["username"],
            csrf_token=csrf_token(),
            left=serialize_job(left),
            right=serialize_job(right),
            rows=align_comparison(summaries[0], summaries[1]),
            status_labels=JOB_STATUS_LABELS,
        )

    @app.get("/api/jobs")
    @login_required
    def list_jobs():
        with connection() as db:
            rows = db.execute(
                "SELECT id, module_id, status, assemblers, threads, sample, archive_path, created_at, "
                "updated_at, started_at, completed_at, error "
                "FROM jobs WHERE owner = ? ORDER BY sequence DESC LIMIT 100",
                (session["username"],),
            ).fetchall()
        return jsonify(jobs=[serialize_job(row) for row in rows])

    def load_owned_upload(upload_id: str):
        try:
            canonical = str(uuid.UUID(str(upload_id)))
        except (TypeError, ValueError, AttributeError):
            return None
        with connection() as db:
            return db.execute(
                "SELECT * FROM uploads WHERE id = ? AND owner = ?",
                (canonical, session["username"]),
            ).fetchone()

    def require_completed_upload(upload_id: str) -> sqlite3.Row:
        row = load_owned_upload(upload_id)
        if row is None:
            raise LookupError("找不到对应的上传。")
        verified = (
            row["status"] == "completed"
            and row["sha256"]
            and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
            and valid_fastq_filename(row["filename"])
        )
        if not verified:
            raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
        try:
            validated_dir = app.config["UPLOAD_VALIDATED"] / str(uuid.UUID(str(row["id"])))
            assembled = contained_path(validated_dir, row["assembled_path"] or "")
        except (OSError, ValueError, LookupError):
            raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
        if not assembled.is_file() or assembled.stat().st_size != row["size"]:
            raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
        return row

    def upload_payload(row: sqlite3.Row) -> dict:
        try:
            received = received_indexes(app.config["STAGING_ROOT"], row)
        except LookupError:
            received = []
        return serialize_upload(row, received)

    @app.post("/api/uploads")
    @login_required
    def create_upload():
        require_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return fail("parameter_invalid", parameter_message("上传参数不正确。"), 400)
        if PATH_FIELDS.intersection(payload) or PATH_FIELDS.intersection(request.args):
            return fail("path_rejected", "不能通过请求指定服务器路径。", 400)
        filename = validated_fastq_name(payload.get("filename"))
        if filename is None:
            return fail(
                "fastq_invalid",
                "FASTQ 格式错误。文件名必须是 .fastq、.fq、.fastq.gz 或 .fq.gz，且不能包含路径。",
                400,
            )
        size = parse_byte_size(payload.get("size"))
        if size is None:
            return fail("fastq_invalid", "FASTQ 格式错误。文件是空的或大小不正确。", 400)
        role = payload.get("role")
        if role not in ("r1", "r2"):
            return fail("fastq_invalid", "FASTQ 格式错误。上传必须标明 R1 或 R2。", 400)
        chunk_size = app.config["UPLOAD_CHUNK_SIZE"]
        chunk_count = (size + chunk_size - 1) // chunk_size
        upload_id = str(uuid.uuid4())
        try:
            directory = session_directory(app.config["STAGING_ROOT"], upload_id)
            directory.mkdir(parents=True)
            (directory / "chunks").mkdir()
        except OSError as error:
            return storage_error(error, "Could not create upload staging directory")
        try:
            with connection() as db:
                db.execute(
                    "INSERT INTO uploads (id, owner, role, filename, size, chunk_size, "
                    "chunk_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (
                        upload_id,
                        session["username"],
                        role,
                        filename,
                        size,
                        chunk_size,
                        chunk_count,
                    ),
                )
        except sqlite3.Error:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(upload=upload_payload(load_owned_upload(upload_id))), 201

    @app.get("/api/uploads/<upload_id>")
    @login_required
    def upload_status(upload_id):
        row = load_owned_upload(upload_id)
        if row is None:
            return fail("not_found", "找不到对应的上传。", 404)
        return jsonify(upload=upload_payload(row))

    @app.put("/api/uploads/<upload_id>/chunks/<int:index>")
    @login_required
    def upload_chunk(upload_id, index):
        require_csrf()
        if PATH_FIELDS.intersection(request.args):
            return fail("path_rejected", "不能通过请求指定服务器路径。", 400)
        row = load_owned_upload(upload_id)
        if row is None:
            return fail("not_found", "找不到对应的上传。", 404)
        if row["status"] in ("completed", "consumed"):
            return fail("conflict", "上传已结束，不能再写入分片。", 409)
        if index < 0 or index >= row["chunk_count"]:
            return fail("upload_failed", "文件上传错误。分片序号超出范围。", 400)
        expected = chunk_byte_length(row["size"], row["chunk_size"], index, row["chunk_count"])
        try:
            chunk_dir = session_directory(app.config["STAGING_ROOT"], row["id"]) / "chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            destination = chunk_dir / "{:08d}".format(index)
            if not is_inside(chunk_dir, destination):
                return fail("path_rejected", "不能通过请求指定服务器路径。", 400)
            skipped = destination.is_file() and destination.stat().st_size == expected
            if not skipped:
                write_exact_stream(request.stream, destination, expected)
        except ValueError as error:
            return upload_value_error(error)
        except OSError as error:
            return storage_error(error, "Could not store upload chunk")
        received = received_indexes(app.config["STAGING_ROOT"], row)
        status = "uploading" if received else row["status"]
        with connection() as db:
            db.execute(
                "UPDATE uploads SET status = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND owner = ? AND status IN ('pending', 'uploading', 'failed')",
                (status, row["id"], session["username"]),
            )
        updated = load_owned_upload(row["id"])
        return jsonify(upload=serialize_upload(updated, received), skipped=skipped)

    @app.post("/api/uploads/<upload_id>/complete")
    @login_required
    def complete_upload(upload_id):
        require_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return fail("parameter_invalid", parameter_message("上传参数不正确。"), 400)
        if PATH_FIELDS.intersection(payload) or PATH_FIELDS.intersection(request.args):
            return fail("path_rejected", "不能通过请求指定服务器路径。", 400)
        supplied = payload.get("sha256")
        if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
            return fail("checksum_mismatch", "SHA-256 校验不一致。", 400)
        supplied = supplied.lower()
        row = load_owned_upload(upload_id)
        if row is None:
            return fail("not_found", "找不到对应的上传。", 404)
        if row["status"] == "consumed":
            return fail("conflict", "上传已结束，不能再写入分片。", 409)
        if row["status"] == "completed":
            if row["sha256"] == supplied:
                return jsonify(upload=upload_payload(row))
            return fail("checksum_mismatch", "SHA-256 校验不一致。", 400)
        try:
            directory = session_directory(app.config["STAGING_ROOT"], row["id"])
            extension = fastq_extension(row["filename"])
            staged = directory / ("read" + extension)
            if not is_inside(directory, staged):
                return fail("path_rejected", "不能通过请求指定服务器路径。", 400)
            digest = assemble_upload(
                directory / "chunks",
                row["size"],
                row["chunk_size"],
                row["chunk_count"],
                staged,
            )
        except LookupError:
            return fail("not_found", "找不到对应的上传。", 404)
        except ValueError as error:
            return upload_value_error(error)
        except OSError as error:
            return storage_error(error, "Could not assemble upload")
        if not hmac.compare_digest(digest, supplied):
            staged.unlink(missing_ok=True)
            with connection() as db:
                db.execute(
                    "UPDATE uploads SET status = 'failed', sha256 = NULL, assembled_path = NULL, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND owner = ?",
                    (row["id"], session["username"]),
                )
            return fail("checksum_mismatch", "SHA-256 校验不一致。", 400)
        try:
            check_fastq_records(staged, app.config.get("FASTQ_CHECK_COMMAND"))
        except FastqRecordError:
            staged.unlink(missing_ok=True)
            with connection() as db:
                db.execute(
                    "UPDATE uploads SET status = 'failed', sha256 = NULL, assembled_path = NULL, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND owner = ?",
                    (row["id"], session["username"]),
                )
            return fail("fastq_invalid", FASTQ_RECORDS, 400)
        except FastqCheckUnavailable:
            staged.unlink(missing_ok=True)
            app.logger.exception("Could not check uploaded FASTQ records")
            return fail("resource_unavailable", FASTQ_CHECK_UNAVAILABLE, 503)
        validated_dir = app.config["UPLOAD_VALIDATED"] / directory.name
        destination = validated_dir / staged.name
        try:
            if not is_inside(app.config["UPLOAD_VALIDATED"], destination):
                raise ValueError("path is outside the data root")
            relocate_file(staged, destination)
        except OSError as error:
            staged.unlink(missing_ok=True)
            return storage_error(error, "Could not store validated upload")
        try:
            with connection() as db:
                cursor = db.execute(
                    "UPDATE uploads SET status = 'completed', sha256 = ?, assembled_path = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND owner = ? AND status IN ('pending', 'uploading', 'failed')",
                    (digest, str(destination), row["id"], session["username"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("上传已结束，不能再写入分片。")
        except (sqlite3.Error, ValueError):
            destination.unlink(missing_ok=True)
            raise
        shutil.rmtree(directory / "chunks", ignore_errors=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        updated = load_owned_upload(row["id"])
        return jsonify(upload=serialize_upload(updated, []))

    @app.post("/api/jobs")
    @login_required
    def create_job():
        require_csrf()
        if "threads" in request.values or "memory" in request.values:
            return fail("parameter_invalid", PARAMETER_THREADS, 400)
        selected_assemblers = request.form.getlist("assembler")
        if not selected_assemblers or any(item not in ASSEMBLERS for item in selected_assemblers):
            return fail("parameter_invalid", parameter_message("请至少选择一种有效的组装软件。"), 400)
        if len(set(selected_assemblers)) != len(selected_assemblers):
            return fail("parameter_invalid", parameter_message("同一种组装软件不能选择两次。"), 400)
        try:
            assembler_list = json.loads(request.form.get("assembler_order", "[]"))
        except json.JSONDecodeError:
            return fail("parameter_invalid", parameter_message("组装软件的顺序不正确。"), 400)
        if (
            not isinstance(assembler_list, list)
            or any(item not in ASSEMBLERS for item in assembler_list)
            or any(not isinstance(item, str) for item in assembler_list)
            or len(assembler_list) != len(selected_assemblers)
            or set(assembler_list) != set(selected_assemblers)
        ):
            return fail("parameter_invalid", parameter_message("组装软件的顺序与所选工具不一致。"), 400)
        try:
            threads = cpu_share()
        except (OSError, RuntimeError, ValueError):
            app.logger.exception("Could not read CPU allocation")
            return fail("resource_unavailable", RESOURCE_START, 503)
        sample = request.form.get("sample", "sample")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sample):
            return fail("parameter_invalid", parameter_message("样本名包含不支持的字符。"), 400)
        kmers_text = request.form.get("spades_kmers", "").strip()
        if kmers_text and "spades" not in assembler_list:
            return fail("parameter_invalid", parameter_message("使用 SPAdes 参数时必须选择 SPAdes。"), 400)
        if kmers_text and not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", kmers_text):
            return fail("parameter_invalid", PARAMETER_KMER, 400)
        if kmers_text and any(
            int(kmer) > 127 or int(kmer) % 2 == 0 for kmer in kmers_text.split(",")
        ):
            return fail("parameter_invalid", PARAMETER_KMER, 400)
        if request.form.get("spades_careful") and "spades" not in assembler_list:
            return fail("parameter_invalid", parameter_message("使用 SPAdes 参数时必须选择 SPAdes。"), 400)
        if PATH_FIELDS.intersection(request.form) or PATH_FIELDS.intersection(request.args):
            return fail("path_rejected", "不能通过请求指定服务器路径。", 400)

        r1_session = request.form.get("r1_session", "").strip()
        r2_session = request.form.get("r2_session", "").strip()
        claimed = None
        if r1_session or r2_session:
            if not r1_session or not r2_session or r1_session == r2_session:
                return fail("fastq_invalid", "FASTQ 格式错误。R1 和 R2 必须是两个不同的已完成上传。", 400)
            try:
                claimed = (
                    require_completed_upload(r1_session),
                    require_completed_upload(r2_session),
                )
            except LookupError:
                return fail("not_found", "找不到对应的上传。", 404)
            except ValueError as error:
                return upload_value_error(error)
            if Path(claimed[0]["assembled_path"]).resolve() == Path(
                claimed[1]["assembled_path"]
            ).resolve():
                return fail("fastq_invalid", "FASTQ 格式错误。R1 和 R2 必须是两个不同的已完成上传。", 400)
            read_names = tuple(
                "R{}".format(index) + fastq_extension(upload["filename"])
                for index, upload in enumerate(claimed, start=1)
            )
            original_names = (claimed[0]["filename"], claimed[1]["filename"])
            original_sha = (
                checksum_or_none(claimed[0]["sha256"]),
                checksum_or_none(claimed[1]["sha256"]),
            )
        else:
            r1 = request.files.get("r1")
            r2 = request.files.get("r2")
            if not r1 or not r2 or not valid_fastq_filename(r1.filename) or not valid_fastq_filename(r2.filename):
                return fail("fastq_invalid", FASTQ_INVALID, 400)
            read_names = (
                "R1" + fastq_extension(r1.filename),
                "R2" + fastq_extension(r2.filename),
            )
            original_names = (secure_filename(r1.filename), secure_filename(r2.filename))
            original_sha = (None, None)
        job_id = str(uuid.uuid4())
        incoming_dir = app.config["UPLOAD_INCOMING"] / job_id
        validated_dir = app.config["UPLOAD_VALIDATED"] / job_id
        job_dir = app.config["JOBS_ROOT"] / job_id
        input_dir = job_dir / "input"
        work_dir = job_dir / "work"
        output_dir = job_dir / "output"
        logs_dir = job_dir / "logs"
        incoming_paths = [incoming_dir / name for name in read_names]
        validated_paths = [validated_dir / name for name in read_names]
        input_paths = [input_dir / name for name in read_names]
        log_path = logs_dir / "job.log"
        stored_files = [*incoming_paths, *validated_paths, *input_paths, log_path]
        stored_dirs = [
            logs_dir,
            output_dir,
            work_dir,
            input_dir,
            job_dir,
            validated_dir,
            incoming_dir,
        ]
        restored = []

        def restore_staged_reads():
            for destination, source in reversed(restored):
                if destination.is_file():
                    relocate_file(destination, source)

        try:
            if claimed:
                for directory in (input_dir, work_dir, output_dir, logs_dir):
                    directory.mkdir(parents=True)
                for upload, destination in zip(claimed, input_paths):
                    source = contained_path(
                        app.config["UPLOAD_VALIDATED"], upload["assembled_path"]
                    )
                    if not source.is_file() or not source.stat().st_size:
                        raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
                    relocate_file(source, destination)
                    restored.append((destination, source))
            else:
                incoming_dir.mkdir(parents=True)
                r1.save(incoming_paths[0])
                r2.save(incoming_paths[1])
                if (
                    not incoming_paths[0].is_file()
                    or not incoming_paths[1].is_file()
                    or not incoming_paths[0].stat().st_size
                    or not incoming_paths[1].stat().st_size
                ):
                    raise ValueError("FASTQ uploads cannot be empty.")
                for incoming in incoming_paths:
                    check_fastq_records(incoming, app.config.get("FASTQ_CHECK_COMMAND"))
                validated_dir.mkdir(parents=True)
                for source, destination in zip(incoming_paths, validated_paths):
                    relocate_file(source, destination)
                incoming_dir.rmdir()
                for directory in (input_dir, work_dir, output_dir, logs_dir):
                    directory.mkdir(parents=True)
                for source, destination in zip(validated_paths, input_paths):
                    relocate_file(source, destination)
                validated_dir.rmdir()
            log_path.write_text(
                "MODULE: {}\nSTATUS: queued\n".format(BACTERIAL_ASSEMBLY_MODULE_ID),
                encoding="utf-8",
            )
            with connection() as db:
                sample_id = reuse_or_create_sample(db, session["username"], sample)
                batch_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO sequencing_batches (id, owner, sample_id, r1_filename, "
                    "r2_filename, r1_sha256, r2_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        session["username"],
                        sample_id,
                        original_names[0],
                        original_names[1],
                        original_sha[0],
                        original_sha[1],
                    ),
                )
                db.execute(
                    "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
                    "sample, spades_kmers, spades_careful, input_r1, input_r2, "
                    "output_dir, log_path, sample_id, sequencing_batch_id) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        session["username"],
                        BACTERIAL_ASSEMBLY_MODULE_ID,
                        json.dumps(assembler_list),
                        threads,
                        sample,
                        kmers_text or None,
                        int(bool(request.form.get("spades_careful"))),
                        str(input_paths[0]),
                        str(input_paths[1]),
                        str(output_dir),
                        str(log_path),
                        sample_id,
                        batch_id,
                    ),
                )
                if claimed:
                    for upload, destination in zip(claimed, input_paths):
                        cursor = db.execute(
                            "UPDATE uploads SET status = 'consumed', assembled_path = ?, "
                            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                            "WHERE id = ? AND owner = ? AND status = 'completed'",
                            (str(destination), upload["id"], session["username"]),
                        )
                        if cursor.rowcount != 1:
                            raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
                note_job(db, session["username"], sample, job_id, "submitted")
        except OSError as error:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            return storage_error(error, "Could not save uploaded FASTQ files")
        except FastqCheckUnavailable:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            app.logger.exception("Could not check uploaded FASTQ records")
            return fail("resource_unavailable", FASTQ_CHECK_UNAVAILABLE, 503)
        except ValueError as error:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            return upload_value_error(error)
        except sqlite3.Error:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            raise
        if claimed:
            for upload in claimed:
                try:
                    session_directory(app.config["STAGING_ROOT"], upload["id"]).rmdir()
                except OSError:
                    pass
        return jsonify(job={"id": job_id, "status": "queued"}), 202

    @app.post("/api/jobs/<job_id>/cancel")
    @login_required
    def cancel_job(job_id):
        require_csrf()
        try:
            uuid.UUID(job_id)
        except ValueError:
            abort(404)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE id = ? AND owner = ?",
                (job_id, session["username"]),
            ).fetchone()
            if row is None:
                abort(404)
            status = row["status"]
            if status in ("succeeded", "failed", "cancelled", "cancelling"):
                return fail("cancel_rejected", "这个任务已经不能取消。", 409)
            if status == "queued":
                updated = db.execute(
                    "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, "
                    "cancel_requested_at = ?, updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND owner = ? AND status = 'queued'",
                    (timestamp, timestamp, timestamp, job_id, session["username"]),
                )
                if updated.rowcount == 1:
                    note_job(db, row["owner"], row["sample"], job_id, "cancelled")
                    try:
                        append_job_log(
                            contained_path(app.config["DATA_ROOT"], row["log_path"]),
                            "STATUS: cancelled",
                        )
                    except ValueError:
                        pass
                    fresh = db.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    return jsonify(job=serialize_job(fresh)), 200
                row = db.execute(
                    "SELECT * FROM jobs WHERE id = ? AND owner = ?",
                    (job_id, session["username"]),
                ).fetchone()
                status = row["status"] if row else None
                if status in ("succeeded", "failed", "cancelled", "cancelling"):
                    return fail("cancel_rejected", "这个任务已经不能取消。", 409)
            updated = db.execute(
                "UPDATE jobs SET cancel_requested = 1, cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND owner = ? AND status = 'running'",
                (timestamp, timestamp, job_id, session["username"]),
            )
            if updated.rowcount != 1:
                return fail("cancel_rejected", "这个任务已经不能取消。", 409)
            fresh = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return jsonify(job=serialize_job(fresh)), 202

    @app.get("/jobs/<job_id>")
    @login_required
    def job_page(job_id):
        row = owned_job(job_id)
        return render_template(
            "job.html",
            username=session["username"],
            csrf_token=csrf_token(),
            job=serialize_job(row),
            progress=job_progress(row),
            detail=job_detail(row),
        )

    @app.get("/api/jobs/<job_id>")
    @login_required
    def job_status(job_id):
        row = owned_job(job_id)
        job = serialize_job(row)
        job["progress"] = job_progress(row)
        job["detail"] = job_detail(row)
        return jsonify(job=job)

    @app.get("/jobs/<job_id>/report")
    @login_required
    def job_report(job_id):
        row = owned_job(job_id)
        report = job_report_model(row)
        if report is None:
            abort(404, description="这份任务还没有可导出的结果。")
        return render_template(
            "report.html",
            username=session["username"],
            csrf_token=csrf_token(),
            job=serialize_job(row),
            report=report,
        )

    @app.get("/api/jobs/<job_id>/reports/<kind>")
    @login_required
    def download_job_report(job_id, kind):
        if kind not in ("html", "tsv"):
            abort(404)
        row = owned_job(job_id)
        report = job_report_model(row)
        if report is None:
            abort(404, description="这份任务还没有可导出的结果。")
        html = render_template("report_document.html", report=report)
        try:
            html_path, tsv_path = store_report_files(row["id"], html, report_tsv(report))
        except OSError as error:
            if getattr(error, "errno", None) in (errno.ELOOP, errno.EPERM):
                abort(404)
            return storage_error(error, "Could not store job report")
        path = html_path if kind == "html" else tsv_path
        suffix = "html" if kind == "html" else "tsv"
        mimetype = "text/html; charset=utf-8" if kind == "html" else "text/plain; charset=utf-8"
        return send_regular_report(
            path,
            download_name="assembly-report-{}.{}".format(row["id"][:8], suffix),
            mimetype=mimetype,
        )

    @app.get("/api/jobs/<job_id>/log")
    @login_required
    def job_log(job_id):
        row = owned_job(job_id)
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return fail("parameter_invalid", parameter_message("日志偏移必须是非负整数。"), 400)
        log_path = data_file(row["log_path"])
        if log_path is None:
            return jsonify(text="", offset=0, status=row["status"])
        with log_path.open("rb") as log_file:
            log_file.seek(min(offset, log_path.stat().st_size))
            chunk = log_file.read(128 * 1024)
            next_offset = log_file.tell()
        return jsonify(
            text=chunk.decode("utf-8", errors="replace"),
            offset=next_offset,
            status=row["status"],
        )

    @app.get("/api/jobs/<job_id>/log/download")
    @login_required
    def download_job_log(job_id):
        row = owned_job(job_id)
        log_path = data_file(row["log_path"])
        if log_path is None:
            abort(404, description="Job log is not available")
        return send_file(
            log_path,
            as_attachment=True,
            download_name="{}-{}.log".format(row["sample"], job_id[:8]),
            mimetype="text/plain; charset=utf-8",
        )

    @app.get("/api/jobs/<job_id>/download")
    @login_required
    def download_job(job_id):
        row = owned_job(job_id)
        if not row["archive_path"]:
            abort(409, description="Results are not ready for download")
        archive = data_file(row["archive_path"])
        if archive is None:
            abort(404)
        return send_file(
            archive,
            as_attachment=True,
            download_name="assembly-{}.tar.gz".format(job_id),
            mimetype="application/gzip",
        )

    @app.get("/api/jobs/<job_id>/files")
    @login_required
    def download_job_file(job_id):
        row = owned_job(job_id)
        path = resolve_output_file(row["output_dir"], request.args.get("name", ""))
        download_name = secure_filename(path.name) or "result"
        return send_file(
            path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/octet-stream",
        )

    def rerun_response(payload: dict, status_code: int):
        wants_json = bool(request.headers.get("X-CSRF-Token")) or (
            "application/json" in request.headers.get("Accept", "")
        )
        if wants_json:
            return jsonify(payload), status_code
        if status_code >= 400:
            flash(payload.get("error") or "不能重新分析。")
        else:
            flash("已重新分析。新任务已排队，原来的结果还在。")
        return redirect(url_for("job_history"))

    @app.post("/api/jobs/<job_id>/rerun")
    @login_required
    def rerun_job(job_id):
        require_csrf()
        row = owned_job(job_id)
        try:
            assemblers = json.loads(row["assemblers"])
        except (TypeError, json.JSONDecodeError):
            assemblers = None
        if not isinstance(assemblers, list) or not assemblers:
            return rerun_response({"error": "原来的参数不完整，不能重新分析。"}, 409)
        if not inputs_ready(row):
            return rerun_response({"error": "原来的测序文件已经不在，不能重新分析。"}, 409)

        new_id = str(uuid.uuid4())
        kept_sample_id = column_value(row, "sample_id")
        kept_batch_id = column_value(row, "sequencing_batch_id")
        job_dir = app.config["JOBS_ROOT"] / new_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        log_path = job_dir / "logs" / "job.log"
        threads = cpu_share()
        try:
            for directory in (input_dir, job_dir / "work", output_dir, log_path.parent):
                directory.mkdir(parents=True)
            input_r1, input_r2 = copy_job_inputs(row, input_dir)
            log_path.write_text(
                "MODULE: {}\nSTATUS: queued\nRERUN OF: {}\n".format(row["module_id"], row["id"]),
                encoding="utf-8",
            )
            with connection() as db:
                db.execute(
                    "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
                    "sample, spades_kmers, spades_careful, input_r1, input_r2, "
                    "output_dir, log_path, rerun_of, sample_id, sequencing_batch_id) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id,
                        session["username"],
                        row["module_id"],
                        json.dumps(assemblers),
                        threads,
                        row["sample"],
                        row["spades_kmers"],
                        int(row["spades_careful"]),
                        str(input_r1),
                        str(input_r2),
                        str(output_dir),
                        str(log_path),
                        row["id"],
                        kept_sample_id,
                        kept_batch_id,
                    ),
                )
        except FileNotFoundError:
            shutil.rmtree(job_dir, ignore_errors=True)
            return rerun_response({"error": "原来的测序文件已经不在，不能重新分析。"}, 409)
        except OSError:
            shutil.rmtree(job_dir, ignore_errors=True)
            app.logger.exception("Could not copy reads for a new analysis")
            return rerun_response({"error": "新任务没有保存成功，请检查磁盘空间。"}, 507)
        except sqlite3.Error:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        return rerun_response(
            {"job": {"id": new_id, "status": "queued", "rerun_of": row["id"]}},
            202,
        )

    def notice_href(job_id: str | None) -> str:
        if job_id and "job_page" in app.view_functions:
            return url_for("job_page", job_id=job_id)
        return url_for("job_history")

    @app.get("/api/notices")
    @login_required
    def list_notices():
        owner = session["username"]
        with connection() as db:
            rows = db.execute(
                "SELECT id, job_id, kind, message, created_at, read_at FROM notices "
                "WHERE owner = ? ORDER BY id DESC LIMIT 40",
                (owner,),
            ).fetchall()
            unread = db.execute(
                "SELECT COUNT(*) FROM notices WHERE owner = ? AND read_at IS NULL",
                (owner,),
            ).fetchone()[0]
        return jsonify(
            unread=unread,
            notices=[
                {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "kind": row["kind"],
                    "message": row["message"],
                    "created_at": row["created_at"],
                    "read": row["read_at"] is not None,
                    "href": notice_href(row["job_id"]),
                }
                for row in rows
            ],
        )

    @app.post("/api/notices/read")
    @login_required
    def read_notices():
        require_csrf()
        with connection() as db:
            db.execute(
                "UPDATE notices SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE owner = ? AND read_at IS NULL",
                (session["username"],),
            )
        return jsonify(ok=True)

    def public_server_error():
        if request.path.startswith("/api/"):
            return fail("internal_error", INTERNAL, 500)
        return render_template("error.html", message=INTERNAL), 500

    @app.errorhandler(413)
    def request_too_large(_error):
        return fail("upload_failed", "文件上传错误。上传超过了服务器允许的大小。", 413)

    @app.errorhandler(500)
    def internal_server_error(error):
        original = getattr(error, "original_exception", None)
        app.logger.error("Request failed", exc_info=original or error)
        return public_server_error()

    @app.errorhandler(Exception)
    def unhandled_error(error):
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("Unhandled request error")
        return public_server_error()

    return app


def is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def session_directory(staging_root: Path, upload_id: str) -> Path:
    try:
        parsed = uuid.UUID(str(upload_id))
    except (TypeError, ValueError, AttributeError):
        raise LookupError("Upload not found")
    root = staging_root.resolve()
    directory = (root / str(parsed)).resolve()
    if directory == root or not is_inside(root, directory):
        raise LookupError("Upload not found")
    return directory


def validated_fastq_name(filename: object) -> str | None:
    if not isinstance(filename, str) or not filename or len(filename) > 240:
        return None
    if any(mark in filename for mark in ("..", "/", "\\", "\x00")):
        return None
    safe = secure_filename(filename)
    if safe.lower() != filename.lower() or not valid_fastq_filename(safe):
        return None
    return safe


def parse_byte_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        size = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value):
        size = int(value)
    else:
        return None
    if size < 1 or size > 2**62:
        return None
    return size


def chunk_byte_length(total: int, chunk_size: int, index: int, chunk_count: int) -> int:
    if index < 0 or index >= chunk_count:
        raise ValueError("分片序号超出范围。")
    if index == chunk_count - 1:
        return total - chunk_size * (chunk_count - 1)
    return chunk_size


def write_exact_stream(stream, destination: Path, expected: int) -> None:
    partial = destination.with_name(destination.name + ".part")
    written = 0
    try:
        with partial.open("wb") as handle:
            while written < expected:
                block = stream.read(min(1024 * 1024, expected - written))
                if not block:
                    break
                handle.write(block)
                written += len(block)
            extra = stream.read(1)
        if extra or written != expected:
            raise ValueError("分片大小与上传会话不一致。")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def assemble_upload(
    chunk_dir: Path, total: int, chunk_size: int, chunk_count: int, destination: Path
) -> str:
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        with partial.open("wb") as output:
            for index in range(chunk_count):
                expected = chunk_byte_length(total, chunk_size, index, chunk_count)
                source = chunk_dir / "{:08d}".format(index)
                if (
                    not source.is_file()
                    or source.stat().st_size != expected
                    or not is_inside(chunk_dir, source)
                ):
                    raise ValueError("仍有分片未上传。")
                with source.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        output.write(block)
        if partial.stat().st_size != total:
            raise ValueError("仍有分片未上传。")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def received_indexes(staging_root: Path, row: sqlite3.Row) -> list[int]:
    chunk_dir = session_directory(staging_root, row["id"]) / "chunks"
    found = []
    if not chunk_dir.is_dir():
        return found
    for index in range(row["chunk_count"]):
        expected = chunk_byte_length(row["size"], row["chunk_size"], index, row["chunk_count"])
        path = chunk_dir / "{:08d}".format(index)
        try:
            if path.is_file() and path.stat().st_size == expected and is_inside(chunk_dir, path):
                found.append(index)
        except OSError:
            continue
    return found


def serialize_upload(row: sqlite3.Row, received: list[int]) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "filename": row["filename"],
        "size": row["size"],
        "chunk_size": row["chunk_size"],
        "chunk_count": row["chunk_count"],
        "received": received,
        "status": row["status"],
        "sha256": row["sha256"],
    }


def host_view(data_root: Path) -> dict:
    view = {}
    for name, metric in snapshot(data_root).items():
        if not metric.get("ok"):
            view[name] = {"ok": False}
            continue
        item = {"ok": True, "percent": metric["percent"]}
        if "cores" in metric:
            item["cores"] = metric["cores"]
        if "used_bytes" in metric:
            item["used"] = format_bytes(metric["used_bytes"])
            item["total"] = format_bytes(metric["total_bytes"])
        if "path" in metric:
            item["path"] = metric["path"]
        view[name] = item
    return view


def parse_filter_date(value: str | None):
    if value is None or value == "":
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        abort(400, description="日期范围不正确。")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description="日期范围不正确。")


def output_name_rejected(name: str) -> bool:
    if not name or any(ord(char) < 32 for char in name):
        return True
    if name.startswith(("/", "\\")) or "\\" in name or ".." in name:
        return True
    path = PurePosixPath(name)
    if path.is_absolute():
        return True
    parts = path.parts
    return not parts or any(part in ("", ".", "..") for part in parts)


def resolve_output_file(output_dir: str, name: str) -> Path:
    if output_name_rejected(name):
        abort(400, description="不能下载这个路径。")
    try:
        root = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        abort(404)
    if root.is_symlink() or not root.is_dir():
        abort(404)
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        try:
            if current.is_symlink():
                abort(400, description="不能下载这个路径。")
            if not current.exists():
                abort(404)
        except OSError:
            abort(404)
    if current.is_symlink() or not is_inside(root, current):
        abort(400, description="不能下载这个路径。")
    if not current.is_file():
        abort(404)
    return current


def notable_output_files(output_dir: str) -> list[str]:
    try:
        root = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        return []
    if root.is_symlink() or not root.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [item for item in dirnames if not (current / item).is_symlink()]
        for filename in filenames:
            path = current / filename
            if path.is_symlink() or not path.is_file():
                continue
            lowered = filename.lower()
            if lowered in RESULT_FILENAMES or lowered.endswith(RESULT_SUFFIXES):
                found.append(path.relative_to(root).as_posix())
    found.sort()
    return found


FASTP_JSON_LIMIT = 20 * 1024 * 1024


def pipeline_directories(output_dir: str) -> list[Path]:
    try:
        output = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        return []
    found = []
    root = current_app.config["DATA_ROOT"].resolve()
    for candidate in (output.parent / "work", output):
        try:
            resolved = candidate.resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                continue
            if resolved.is_symlink() or not resolved.is_dir():
                continue
        except OSError:
            continue
        if resolved not in found:
            found.append(resolved)
    return found


def read_regular_text(directory: Path, relative: str, limit: int) -> str | None:
    path = directory.joinpath(*PurePosixPath(relative).parts)
    try:
        if path.is_symlink() or not path.is_file() or not is_inside(directory, path):
            return None
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def first_pipeline_text(directories: list[Path], relative: str, limit: int) -> str | None:
    for directory in directories:
        text = read_regular_text(directory, relative, limit)
        if text and text.strip():
            return text
    return None


def load_fastp_documents(directories: list[Path]) -> list[dict]:
    documents = []
    seen = set()
    for directory in directories:
        folder = directory / "fastp"
        try:
            if folder.is_symlink() or not folder.is_dir() or not is_inside(directory, folder):
                continue
            names = sorted(path.name for path in folder.iterdir())
        except OSError:
            continue
        for name in names:
            if not name.endswith("_fastp.json") or name in seen:
                continue
            path = folder / name
            try:
                if path.is_symlink() or not path.is_file() or not is_inside(folder, path):
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            seen.add(name)
            if size <= 0:
                continue
            if size > FASTP_JSON_LIMIT:
                documents.append(
                    {"name": name, "present": True, "unreadable": True, "payload": None}
                )
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                documents.append(
                    {"name": name, "present": True, "unreadable": True, "payload": None}
                )
                continue
            if not isinstance(payload, dict):
                documents.append(
                    {"name": name, "present": True, "unreadable": True, "payload": None}
                )
                continue
            documents.append(
                {"name": name, "present": True, "unreadable": False, "payload": payload}
            )
    return documents


def stage_failures(row: sqlite3.Row) -> list[dict]:
    try:
        output_dir = contained_path(current_app.config["DATA_ROOT"], row["output_dir"])
        record_path = stage_record_path(output_dir)
    except (OSError, ValueError):
        return []
    failures = []
    for stage in load_stage_record(record_path).get("stages") or []:
        if not isinstance(stage, dict) or stage.get("state") != "failed":
            continue
        item = {"id": stage.get("id"), "label": stage.get("label") or stage.get("id")}
        detail = stage.get("detail")
        if isinstance(detail, str) and detail.strip():
            item["detail"] = detail.strip()
        failures.append(item)
    return failures


def input_facts(row: sqlite3.Row) -> list[dict]:
    facts = []
    for role, key in (("R1", "input_r1"), ("R2", "input_r2")):
        item = {"role": role, "name": None, "bytes": None}
        raw = column_value(row, key)
        if raw:
            try:
                path = contained_path(current_app.config["DATA_ROOT"], raw)
            except (OSError, ValueError):
                path = None
            if path is not None:
                item["name"] = path.name
                try:
                    if not path.is_symlink() and path.is_file():
                        item["bytes"] = path.stat().st_size
                except OSError:
                    item["bytes"] = None
        facts.append(item)
    return facts


def write_regular_file(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise OSError(errno.ELOOP, "report path is a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def store_report_files(job_id: str, html: str, tsv: str) -> tuple[Path, Path]:
    root = Path(current_app.config["REPORTS_ROOT"]).resolve()
    directory = root / str(uuid.UUID(str(job_id)))
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise OSError(errno.ELOOP, "report directory is not a real directory")
    if directory != root and root not in directory.resolve(strict=False).parents:
        raise OSError(errno.EPERM, "report directory escapes the reports root")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or not is_inside(root, directory):
        raise OSError(errno.ELOOP, "report directory is not a real directory")
    html_path = directory / "report.html"
    tsv_path = directory / "report.tsv"
    write_regular_file(html_path, html.encode("utf-8"))
    write_regular_file(tsv_path, tsv.encode("utf-8"))
    return html_path, tsv_path


def send_regular_report(path: Path, download_name: str, mimetype: str):
    root = Path(current_app.config["REPORTS_ROOT"]).resolve()
    if path.is_symlink() or not path.is_file() or not is_inside(root, path):
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype,
    )


def comparison_file(output_dir: str) -> Path | None:
    try:
        root = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        return None
    candidate = root / "comparison.tsv"
    try:
        if candidate.is_symlink() or not candidate.is_file() or not is_inside(root, candidate):
            return None
    except OSError:
        return None
    return candidate


def inputs_ready(row) -> bool:
    seen = []
    for key in ("input_r1", "input_r2"):
        raw = row[key] if key in row.keys() else None
        if not raw:
            return False
        try:
            path = contained_path(current_app.config["DATA_ROOT"], raw)
            if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                return False
        except (OSError, ValueError):
            return False
        seen.append(path)
    return len(seen) == 2


def copy_job_inputs(row, input_dir: Path) -> tuple[Path, Path]:
    copied = []
    for index, key in enumerate(("input_r1", "input_r2"), start=1):
        source = contained_path(current_app.config["DATA_ROOT"], row[key])
        if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
            raise FileNotFoundError(key)
        destination = input_dir / "R{}{}".format(index, "".join(source.suffixes))
        if not is_inside(input_dir, destination):
            raise ValueError("path is outside the data root")
        shutil.copyfile(source, destination)
        copied.append(destination)
    return copied[0], copied[1]


def parse_comparison_tsv(path: Path | None) -> list[dict]:
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_comparison_text(text)


def parse_comparison_text(text: str | None) -> list[dict]:
    if not text or not text.strip():
        return []
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    if not rows:
        return []
    metrics = [cell for cell in rows[0][1:] if cell]
    parsed = []
    for row in rows[1:]:
        if not row or not str(row[0]).strip():
            continue
        values = {}
        metric_index = 0
        for cell_index, metric in enumerate(rows[0][1:], start=1):
            if not metric:
                continue
            values[metrics[metric_index]] = row[cell_index] if cell_index < len(row) else ""
            metric_index += 1
        parsed.append({"assembler": row[0], "metrics": values})
    return parsed


def align_comparison(left: list[dict], right: list[dict]) -> list[dict]:
    metric_order = []
    assembler_order = []
    for side in (left, right):
        for item in side:
            if item["assembler"] not in assembler_order:
                assembler_order.append(item["assembler"])
            for metric in item["metrics"]:
                if metric not in metric_order:
                    metric_order.append(metric)
    left_map = {item["assembler"]: item["metrics"] for item in left}
    right_map = {item["assembler"]: item["metrics"] for item in right}
    aligned = []
    for assembler in assembler_order:
        for metric in metric_order:
            left_metrics = left_map.get(assembler, {})
            right_metrics = right_map.get(assembler, {})
            if metric not in left_metrics and metric not in right_metrics:
                continue
            aligned.append(
                {
                    "assembler": assembler,
                    "metric": metric,
                    "left": left_metrics.get(metric) if metric in left_metrics else None,
                    "right": right_metrics.get(metric) if metric in right_metrics else None,
                }
            )
    return aligned


def parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def valid_fastq_filename(filename: str) -> bool:
    safe_name = secure_filename(filename or "").lower()
    return bool(safe_name) and safe_name.endswith(FASTQ_SUFFIXES)


def fastq_extension(filename: str) -> str:
    lowered = secure_filename(filename).lower()
    return next(suffix for suffix in FASTQ_SUFFIXES if lowered.endswith(suffix))


def job_progress(row: sqlite3.Row) -> dict:
    try:
        output_dir = contained_path(current_app.config["DATA_ROOT"], row["output_dir"])
        record_path = stage_record_path(output_dir)
    except (OSError, RuntimeError, ValueError):
        record_path = current_app.config["DATA_ROOT"] / "missing-stage-record.json"
    record = load_stage_record(record_path)
    progress = build_progress(json.loads(row["assemblers"]), record, row["status"])
    if not progress["updated_at"]:
        progress["updated_at"] = row["updated_at"]
    seconds = elapsed_seconds(row["started_at"], row["completed_at"])
    progress["elapsed_seconds"] = seconds
    progress["elapsed_label"] = (
        "等待开始" if seconds is None else format_duration(seconds)
    )
    return progress


def serialize_job(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "module_id": row["module_id"],
        "status": row["status"],
        "assemblers": json.loads(row["assemblers"]),
        "threads": row["threads"],
        "archive_available": data_file(row["archive_path"]) is not None,
        "sample": row["sample"],
        "rerun_of": row["rerun_of"] if "rerun_of" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "attempt": column_value(row, "attempt") or 0,
        "heartbeat_at": column_value(row, "heartbeat_at"),
        "worker_id": column_value(row, "worker_id"),
        "cancel_requested_at": column_value(row, "cancel_requested_at"),
        "archived_at": column_value(row, "archived_at"),
        "process_group_id": column_value(row, "process_group_id"),
        "process_started_at": column_value(row, "process_started_at"),
        "error": row["error"],
    }


def elapsed_seconds(
    started_at: str | None,
    completed_at: str | None,
    current_time: datetime | None = None,
) -> int | None:
    if not started_at:
        return None

    def parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    started = parse_timestamp(started_at)
    ended = parse_timestamp(completed_at) if completed_at else current_time or datetime.now(timezone.utc)
    return max(0, int((ended - started).total_seconds()))


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{}小时 {}分".format(hours, minutes)
    if minutes:
        return "{}分 {}秒".format(minutes, seconds)
    return "{}秒".format(seconds)


app = create_app() if os.environ.get("ASSEMBLY_WEB_AUTOCREATE", "1") == "1" else None
