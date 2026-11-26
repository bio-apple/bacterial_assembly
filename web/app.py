#!/usr/bin/env python3
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path, PurePosixPath

from flask import (
    Flask,
    abort,
    current_app,
    flash,
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
from web.db_sqlite import (
    SCHEMA,
    ensure_cancellation_schema,
    ensure_lifecycle_columns,
    ensure_partial_success_status,
    ensure_rerun_column,
    ensure_sample_columns,
    relocate_file,
    relocate_legacy_database,
)
from web.fastq_check import FastqCheckUnavailable, FastqRecordError
from web.modules import ANALYSIS_MODULES
from web.notices import note_disk
from web.job_serialize import elapsed_seconds, format_duration
from web.storage_paths import contained_path, is_inside
from web.upload_io import (
    assemble_upload,
    fastq_extension,
    received_indexes,
    serialize_upload,
    session_directory,
    valid_fastq_filename,
    validated_fastq_name,
)
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


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("ASSEMBLY_WEB_SECRET"),
        USERS_FILE=os.environ.get("ASSEMBLY_USERS_FILE", "/config/users.json"),
        DATA_ROOT=os.environ.get("ASSEMBLY_DATA_ROOT", "/data"),
        PIPELINE_PATH=Path(__file__).resolve().parents[1] / "run_pipeline.py",
        ANALYSIS_MODULES=ANALYSIS_MODULES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=env_flag("ASSEMBLY_SESSION_COOKIE_SECURE", True),
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
    if not test_config:
        from web.runtime_config import validate_web_runtime, wire_tool_runtime_hooks

        validate_web_runtime(check_docker=False)
        wire_tool_runtime_hooks()
    else:
        from web.runtime_config import wire_tool_runtime_hooks

        wire_tool_runtime_hooks()
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
        ensure_partial_success_status(db)
        ensure_rerun_column(db)
        ensure_sample_columns(db)
        ensure_lifecycle_columns(db)
        db.executescript(
            "CREATE TABLE IF NOT EXISTS job_steps ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "job_id TEXT NOT NULL, "
            "step_name TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN "
            "('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')), "
            "started_at TEXT, completed_at TEXT, exit_code INTEGER, error TEXT, "
            "UNIQUE(job_id, step_name));"
            "CREATE INDEX IF NOT EXISTS job_steps_job ON job_steps(job_id, id);"
        )
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
        if response.headers.pop("X-Fastp-Report", None) == "1":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        else:
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
                "dashboard": "dashboard",
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
        if any(
            phrase in text
            for phrase in ("记录无法通过检查", "数量不一致", "read 编号", "gzip")
        ):
            return fail("fastq_invalid", text, 400)
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

    from types import SimpleNamespace
    from web.routes import register_blueprints

    hooks = SimpleNamespace(
        connection=connection,
        login_required=login_required,
        require_csrf=require_csrf,
        fail=fail,
        storage_error=storage_error,
        upload_value_error=upload_value_error,
        csrf_token=csrf_token,
        owned_job=owned_job,
        load_owned_upload=load_owned_upload,
        require_completed_upload=require_completed_upload,
        upload_payload=upload_payload,
        PATH_FIELDS=PATH_FIELDS,
    )
    register_blueprints(app, hooks)

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


app = create_app() if os.environ.get("ASSEMBLY_WEB_AUTOCREATE", "1") == "1" else None

