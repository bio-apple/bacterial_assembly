#!/usr/bin/env python3
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
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
from web.modules import ANALYSIS_MODULES, BACTERIAL_ASSEMBLY_MODULE_ID

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
FASTQ_SUFFIXES = (".fastq", ".fq", ".fastq.gz", ".fq.gz")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_owner_sequence ON jobs(owner, sequence DESC);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, sequence);
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
    )
    if test_config:
        app.config.update(test_config)
    if not isinstance(app.config["SECRET_KEY"], str) or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("Set ASSEMBLY_WEB_SECRET to a random secret of at least 32 characters")
    app.config["USERS"] = app.config.get("USERS") or load_users(
        Path(app.config["USERS_FILE"])
    )
    app.config["DATA_ROOT"] = Path(app.config["DATA_ROOT"]).resolve()
    app.config["UPLOAD_ROOT"] = app.config["DATA_ROOT"] / "uploads"
    app.config["RESULT_ROOT"] = app.config["DATA_ROOT"] / "results"
    app.config["DATABASE"] = app.config["DATA_ROOT"] / "jobs.sqlite3"
    app.config["DATA_ROOT"].mkdir(parents=True, exist_ok=True)
    (app.config["DATA_ROOT"] / "tmp").mkdir(parents=True, exist_ok=True)
    app.config["UPLOAD_ROOT"].mkdir(parents=True, exist_ok=True)
    app.config["RESULT_ROOT"].mkdir(parents=True, exist_ok=True)
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
            "active_page": "history" if request.endpoint == "job_history" else "modules",
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
            return redirect(url_for("module_index"))
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
        return redirect(url_for("module_index"))

    @app.post("/logout")
    @login_required
    def logout():
        require_csrf()
        session.clear()
        return redirect(url_for("login"))

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
            job["log_available"] = Path(row["log_path"]).is_file()
            job["result_available"] = (
                bool(row["archive_path"]) and Path(row["archive_path"]).is_file()
            )
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

        r1 = request.files.get("r1")
        r2 = request.files.get("r2")
        if not r1 or not r2 or not valid_fastq_filename(r1.filename) or not valid_fastq_filename(r2.filename):
            return jsonify(error="Upload both R1 and R2 as FASTQ or FASTQ.GZ files."), 400
        job_id = str(uuid.uuid4())
        input_dir = app.config["UPLOAD_ROOT"] / job_id
        result_dir = app.config["RESULT_ROOT"] / job_id
        r1_path = input_dir / ("R1" + fastq_extension(r1.filename))
        r2_path = input_dir / ("R2" + fastq_extension(r2.filename))
        log_path = result_dir / "job.log"
        try:
            input_dir.mkdir(parents=True)
            r1.save(r1_path)
            r2.save(r2_path)
            if not r1_path.stat().st_size or not r2_path.stat().st_size:
                raise ValueError("FASTQ uploads cannot be empty.")
            result_dir.mkdir(parents=True)
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
                        str(r1_path),
                        str(r2_path),
                        str(result_dir),
                        str(log_path),
                    ),
                )
        except OSError:
            for path in (r1_path, r2_path, log_path):
                path.unlink(missing_ok=True)
            for directory in (input_dir, result_dir):
                if directory.exists():
                    directory.rmdir()
            app.logger.exception("Could not save uploaded FASTQ files")
            return jsonify(error="Upload could not be stored; check server disk space."), 507
        except ValueError as error:
            for path in (r1_path, r2_path, log_path):
                path.unlink(missing_ok=True)
            for directory in (input_dir, result_dir):
                if directory.exists():
                    directory.rmdir()
            return jsonify(error=str(error)), 400
        except sqlite3.Error:
            for path in (r1_path, r2_path, log_path):
                path.unlink(missing_ok=True)
            for directory in (input_dir, result_dir):
                if directory.exists():
                    directory.rmdir()
            raise
        return jsonify(job={"id": job_id, "status": "queued"}), 202

    @app.get("/api/jobs/<job_id>")
    @login_required
    def job_status(job_id):
        row = owned_job(job_id)
        return jsonify(job=serialize_job(row))

    @app.get("/api/jobs/<job_id>/log")
    @login_required
    def job_log(job_id):
        row = owned_job(job_id)
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify(error="offset must be a non-negative integer"), 400
        log_path = Path(row["log_path"])
        if not log_path.is_file():
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
        log_path = Path(row["log_path"])
        if not log_path.is_file():
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
        archive = Path(row["archive_path"])
        if not archive.is_file():
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


def serialize_job(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "module_id": row["module_id"],
        "status": row["status"],
        "assemblers": json.loads(row["assemblers"]),
        "threads": row["threads"],
        "archive_available": bool(
            row["archive_path"] and Path(row["archive_path"]).is_file()
        ),
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
