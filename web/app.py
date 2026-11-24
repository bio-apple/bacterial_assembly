#!/usr/bin/env python3
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
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from scripts.common import cpu_share
from scripts.stage_record import build_progress, load_stage_record
from web.host_monitor import format_bytes, snapshot
from web.modules import ANALYSIS_MODULES, BACTERIAL_ASSEMBLY_MODULE_ID

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    completed_at TEXT
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
                "module_index": "modules",
                "job_page": "monitor",
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
                    return jsonify(error="Authentication required"), 401
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

    @app.get("/modules")
    @login_required
    def module_index():
        return render_template(
            "modules.html",
            username=session["username"],
            csrf_token=csrf_token(),
            modules=app.config["ANALYSIS_MODULES"],
        )

    @app.get("/")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            username=session["username"],
            csrf_token=csrf_token(),
            assemblers=ASSEMBLERS,
        )

    @app.get("/history")
    @login_required
    def job_history():
        page = parse_positive_int(request.args.get("page", "1"))
        if page is None:
            abort(400, description="page must be a positive integer")
        page_size = 50
        owner = session["username"]
        with connection() as db:
            total_count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner = ?", (owner,)
            ).fetchone()[0]
            total_pages = max(1, (total_count + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = db.execute(
                "SELECT * FROM jobs WHERE owner = ? ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (owner, page_size, (page - 1) * page_size),
            ).fetchall()

        modules = {module["id"]: module for module in app.config["ANALYSIS_MODULES"]}
        current_time = datetime.now(timezone.utc)
        history_jobs = []
        for row in rows:
            job = serialize_job(row)
            module = modules.get(job["module_id"], {})
            job["module_name"] = module.get("name", job["module_id"])
            job["module_code"] = module.get("code", "—")
            job["log_available"] = data_file(row["log_path"]) is not None
            job["result_available"] = data_file(row["archive_path"]) is not None
            job["runtime_seconds"] = elapsed_seconds(
                job["started_at"], job["completed_at"], current_time
            )
            job["runtime_label"] = (
                "等待开始"
                if job["runtime_seconds"] is None
                else format_duration(job["runtime_seconds"])
            )
            history_jobs.append(job)

        return render_template(
            "history.html",
            username=session["username"],
            csrf_token=csrf_token(),
            jobs=history_jobs,
            page=page,
            total_pages=total_pages,
            total_count=total_count,
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
            return jsonify(error="上传参数不正确。"), 400
        if PATH_FIELDS.intersection(payload) or PATH_FIELDS.intersection(request.args):
            return jsonify(error="不能通过请求指定服务器路径。"), 400
        filename = validated_fastq_name(payload.get("filename"))
        if filename is None:
            return jsonify(error="文件名必须是 .fastq、.fq、.fastq.gz 或 .fq.gz，且不能包含路径。"), 400
        size = parse_byte_size(payload.get("size"))
        if size is None:
            return jsonify(error="文件是空的或大小不正确。"), 400
        role = payload.get("role")
        if role not in ("r1", "r2"):
            return jsonify(error="上传必须标明 R1 或 R2。"), 400
        chunk_size = app.config["UPLOAD_CHUNK_SIZE"]
        chunk_count = (size + chunk_size - 1) // chunk_size
        upload_id = str(uuid.uuid4())
        try:
            directory = session_directory(app.config["STAGING_ROOT"], upload_id)
            directory.mkdir(parents=True)
            (directory / "chunks").mkdir()
        except OSError:
            app.logger.exception("Could not create upload staging directory")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
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
            return jsonify(error="找不到对应的上传。"), 404
        return jsonify(upload=upload_payload(row))

    @app.put("/api/uploads/<upload_id>/chunks/<int:index>")
    @login_required
    def upload_chunk(upload_id, index):
        require_csrf()
        if PATH_FIELDS.intersection(request.args):
            return jsonify(error="不能通过请求指定服务器路径。"), 400
        row = load_owned_upload(upload_id)
        if row is None:
            return jsonify(error="找不到对应的上传。"), 404
        if row["status"] in ("completed", "consumed"):
            return jsonify(error="上传已结束，不能再写入分片。"), 409
        if index < 0 or index >= row["chunk_count"]:
            return jsonify(error="分片序号超出范围。"), 400
        expected = chunk_byte_length(row["size"], row["chunk_size"], index, row["chunk_count"])
        try:
            chunk_dir = session_directory(app.config["STAGING_ROOT"], row["id"]) / "chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            destination = chunk_dir / "{:08d}".format(index)
            if not is_inside(chunk_dir, destination):
                return jsonify(error="不能通过请求指定服务器路径。"), 400
            skipped = destination.is_file() and destination.stat().st_size == expected
            if not skipped:
                write_exact_stream(request.stream, destination, expected)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except OSError:
            app.logger.exception("Could not store upload chunk")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
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
            return jsonify(error="上传参数不正确。"), 400
        if PATH_FIELDS.intersection(payload) or PATH_FIELDS.intersection(request.args):
            return jsonify(error="不能通过请求指定服务器路径。"), 400
        supplied = payload.get("sha256")
        if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
            return jsonify(error="SHA-256 校验不一致。"), 400
        supplied = supplied.lower()
        row = load_owned_upload(upload_id)
        if row is None:
            return jsonify(error="找不到对应的上传。"), 404
        if row["status"] == "consumed":
            return jsonify(error="上传已结束，不能再写入分片。"), 409
        if row["status"] == "completed":
            if row["sha256"] == supplied:
                return jsonify(upload=upload_payload(row))
            return jsonify(error="SHA-256 校验不一致。"), 400
        try:
            directory = session_directory(app.config["STAGING_ROOT"], row["id"])
            extension = fastq_extension(row["filename"])
            staged = directory / ("read" + extension)
            if not is_inside(directory, staged):
                return jsonify(error="不能通过请求指定服务器路径。"), 400
            digest = assemble_upload(
                directory / "chunks",
                row["size"],
                row["chunk_size"],
                row["chunk_count"],
                staged,
            )
        except LookupError:
            return jsonify(error="找不到对应的上传。"), 404
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except OSError:
            app.logger.exception("Could not assemble upload")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
        if not hmac.compare_digest(digest, supplied):
            staged.unlink(missing_ok=True)
            with connection() as db:
                db.execute(
                    "UPDATE uploads SET status = 'failed', sha256 = NULL, assembled_path = NULL, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND owner = ?",
                    (row["id"], session["username"]),
                )
            return jsonify(error="SHA-256 校验不一致。"), 400
        validated_dir = app.config["UPLOAD_VALIDATED"] / directory.name
        destination = validated_dir / staged.name
        try:
            if not is_inside(app.config["UPLOAD_VALIDATED"], destination):
                raise ValueError("path is outside the data root")
            relocate_file(staged, destination)
        except OSError:
            staged.unlink(missing_ok=True)
            app.logger.exception("Could not store validated upload")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
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
            return jsonify(
                error="Thread count is automatic and memory settings are not configurable."
            ), 400
        selected_assemblers = request.form.getlist("assembler")
        if not selected_assemblers or any(item not in ASSEMBLERS for item in selected_assemblers):
            return jsonify(error="Select at least one valid assembler."), 400
        if len(set(selected_assemblers)) != len(selected_assemblers):
            return jsonify(error="An assembler cannot be selected more than once."), 400
        try:
            assembler_list = json.loads(request.form.get("assembler_order", "[]"))
        except json.JSONDecodeError:
            return jsonify(error="Invalid assembler order."), 400
        if (
            not isinstance(assembler_list, list)
            or any(item not in ASSEMBLERS for item in assembler_list)
            or any(not isinstance(item, str) for item in assembler_list)
            or len(assembler_list) != len(selected_assemblers)
            or set(assembler_list) != set(selected_assemblers)
        ):
            return jsonify(error="Assembler order does not match the selected tools."), 400
        threads = cpu_share()
        sample = request.form.get("sample", "sample")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sample):
            return jsonify(error="Sample name contains unsupported characters."), 400
        kmers_text = request.form.get("spades_kmers", "").strip()
        if kmers_text and "spades" not in assembler_list:
            return jsonify(error="SPAdes options require selecting SPAdes."), 400
        if kmers_text and not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", kmers_text):
            return jsonify(error="SPAdes k-mers must be comma-separated integers."), 400
        if kmers_text and any(
            int(kmer) > 127 or int(kmer) % 2 == 0 for kmer in kmers_text.split(",")
        ):
            return jsonify(error="SPAdes k-mers must be odd and at most 127."), 400
        if request.form.get("spades_careful") and "spades" not in assembler_list:
            return jsonify(error="SPAdes options require selecting SPAdes."), 400
        if PATH_FIELDS.intersection(request.form) or PATH_FIELDS.intersection(request.args):
            return jsonify(error="不能通过请求指定服务器路径。"), 400

        r1_session = request.form.get("r1_session", "").strip()
        r2_session = request.form.get("r2_session", "").strip()
        claimed = None
        if r1_session or r2_session:
            if not r1_session or not r2_session or r1_session == r2_session:
                return jsonify(error="R1 和 R2 必须是两个不同的已完成上传。"), 400
            try:
                claimed = (
                    require_completed_upload(r1_session),
                    require_completed_upload(r2_session),
                )
            except LookupError:
                return jsonify(error="找不到对应的上传。"), 404
            except ValueError as error:
                return jsonify(error=str(error)), 400
            if Path(claimed[0]["assembled_path"]).resolve() == Path(
                claimed[1]["assembled_path"]
            ).resolve():
                return jsonify(error="R1 和 R2 必须是两个不同的已完成上传。"), 400
            read_names = tuple(
                "R{}".format(index) + fastq_extension(upload["filename"])
                for index, upload in enumerate(claimed, start=1)
            )
        else:
            r1 = request.files.get("r1")
            r2 = request.files.get("r2")
            if not r1 or not r2 or not valid_fastq_filename(r1.filename) or not valid_fastq_filename(r2.filename):
                return jsonify(error="Upload both R1 and R2 as FASTQ or FASTQ.GZ files."), 400
            read_names = (
                "R1" + fastq_extension(r1.filename),
                "R2" + fastq_extension(r2.filename),
            )
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
                db.execute(
                    "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
                    "sample, spades_kmers, spades_careful, input_r1, input_r2, "
                    "output_dir, log_path) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        except OSError:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            app.logger.exception("Could not save uploaded FASTQ files")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
        except ValueError as error:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            return jsonify(error=str(error)), 400
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
                return jsonify(error="This job can no longer be cancelled."), 409
            if status == "queued":
                updated = db.execute(
                    "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, "
                    "updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND owner = ? AND status = 'queued'",
                    (timestamp, timestamp, job_id, session["username"]),
                )
                if updated.rowcount == 1:
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
                    return jsonify(error="This job can no longer be cancelled."), 409
            updated = db.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND owner = ? AND status = 'running'",
                (timestamp, job_id, session["username"]),
            )
            if updated.rowcount != 1:
                return jsonify(error="This job can no longer be cancelled."), 409
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
        )

    @app.get("/api/jobs/<job_id>")
    @login_required
    def job_status(job_id):
        row = owned_job(job_id)
        job = serialize_job(row)
        job["progress"] = job_progress(row)
        return jsonify(job=job)

    @app.get("/api/jobs/<job_id>/log")
    @login_required
    def job_log(job_id):
        row = owned_job(job_id)
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify(error="offset must be a non-negative integer"), 400
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

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(error="Upload was rejected by the server or proxy size limit."), 413

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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
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
