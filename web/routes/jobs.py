"""Job submit, monitor, and download HTTP handlers."""

from __future__ import annotations

import errno
import json
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Blueprint,
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
from werkzeug.utils import secure_filename

from scripts.resource_budget import read_runtime_limits
from web.analysis_task_intake import (
    checksum_or_none,
    discard_stored_files,
    enqueue_analysis_task,
    insert_new_job_catalog,
    insert_rerun_job_catalog,
)
from web.db_sqlite import relocate_file
from web.errors import (
    FASTQ_CHECK_UNAVAILABLE,
    FASTQ_INVALID,
    PARAMETER_KMER,
    PARAMETER_THREADS,
    RESOURCE_START,
    parameter_message,
)
from web.fastq_check import FastqCheckUnavailable
from web.ingest import validate_fastq_for_analysis, validate_read_pair_for_job
from web.job_artifacts import (
    copy_job_inputs,
    fastp_html_file,
    inputs_ready,
    pipeline_directories,
    resolve_output_file,
)
from web.job_presentation import job_detail, job_report_model
from web.job_serialize import ASSEMBLERS, data_file, serialize_job
from web.modules import BACTERIAL_ASSEMBLY_MODULE_ID
from web.notices import note_job
from web.report_io import send_regular_report, store_report_files
from web.reports import report_tsv
from web.step_read_model import job_step_view
from web.storage_paths import append_job_log, contained_path
from web.upload_io import (
    fastq_extension,
    session_directory,
    valid_fastq_filename,
)


def register_routes(bp: Blueprint, hooks) -> None:
    login_required = hooks.login_required
    require_csrf = hooks.require_csrf
    fail = hooks.fail
    storage_error = hooks.storage_error
    upload_value_error = hooks.upload_value_error
    csrf_token = hooks.csrf_token
    connection = hooks.connection
    owned_job = hooks.owned_job
    require_completed_upload = hooks.require_completed_upload
    path_fields = hooks.PATH_FIELDS

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

    @bp.post("/api/jobs", endpoint="create_job")
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
            threads = read_runtime_limits()["method_threads"]
        except (OSError, RuntimeError, ValueError):
            current_app.logger.exception("Could not read CPU allocation")
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
        if path_fields.intersection(request.form) or path_fields.intersection(request.args):
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
        incoming_dir = current_app.config["UPLOAD_INCOMING"] / job_id
        validated_dir = current_app.config["UPLOAD_VALIDATED"] / job_id
        job_dir = current_app.config["JOBS_ROOT"] / job_id
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
                        current_app.config["UPLOAD_VALIDATED"], upload["assembled_path"]
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
                    validate_fastq_for_analysis(
                        incoming,
                        current_app.config.get("FASTQ_CHECK_COMMAND"),
                        log_dir=current_app.config["TEMP_ROOT"] / "fastq-checks" / job_id,
                    )
                validated_dir.mkdir(parents=True)
                for source, destination in zip(incoming_paths, validated_paths):
                    relocate_file(source, destination)
                incoming_dir.rmdir()
                for directory in (input_dir, work_dir, output_dir, logs_dir):
                    directory.mkdir(parents=True)
                for source, destination in zip(validated_paths, input_paths):
                    relocate_file(source, destination)
                validated_dir.rmdir()
            pair_log = current_app.config["TEMP_ROOT"] / "fastq-checks" / job_id
            validate_read_pair_for_job(input_paths[0], input_paths[1], log_dir=pair_log)
            for name in ("fastq_pair.log", "fastq_pair.json"):
                source = pair_log / name
                if source.is_file():
                    relocate_file(source, logs_dir / name)
            log_path.write_text(
                "MODULE: {}\nSTATUS: queued\n".format(BACTERIAL_ASSEMBLY_MODULE_ID),
                encoding="utf-8",
            )
            with connection() as db:
                insert_new_job_catalog(
                    db,
                    owner=session["username"],
                    sample=sample,
                    job_id=job_id,
                    assembler_list=assembler_list,
                    threads=threads,
                    kmers_text=kmers_text or None,
                    spades_careful=bool(request.form.get("spades_careful")),
                    input_paths=(input_paths[0], input_paths[1]),
                    output_dir=output_dir,
                    log_path=log_path,
                    original_names=original_names,
                    original_sha=original_sha,
                    claimed=claimed,
                )
        except OSError as error:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            return storage_error(error, "Could not save uploaded FASTQ files")
        except FastqCheckUnavailable:
            restore_staged_reads()
            discard_stored_files(stored_files, stored_dirs)
            current_app.logger.exception("Could not check uploaded FASTQ records")
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
                    session_directory(current_app.config["STAGING_ROOT"], upload["id"]).rmdir()
                except OSError:
                    pass
        try:
            enqueue_analysis_task(
                job_id=job_id,
                owner=session["username"],
                sample=sample,
                assemblers=assembler_list,
                threads=threads,
                spades_kmers=kmers_text or None,
                spades_careful=bool(request.form.get("spades_careful")),
                input_r1=str(input_paths[0]),
                input_r2=str(input_paths[1]),
                output_dir=str(output_dir),
                log_path=str(log_path),
            )
        except Exception:
            current_app.logger.exception("Could not enqueue RQ pipeline for %s", job_id)
            return fail("resource_unavailable", "无法启动分析队列，请稍后重试。", 503)
        return jsonify(job={"id": job_id, "status": "queued"}), 202

    @bp.post("/api/jobs/<job_id>/cancel", endpoint="cancel_job")
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
            if status in ("succeeded", "partial_success", "failed", "cancelled", "cancelling"):
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
                            contained_path(current_app.config["DATA_ROOT"], row["log_path"]),
                            "STATUS: cancelled",
                        )
                    except ValueError:
                        pass
                    fresh = db.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    try:
                        from web.cancel import request_cancel

                        request_cancel(job_id)
                    except Exception:
                        current_app.logger.exception("RQ cancel failed for %s", job_id)
                    return jsonify(job=serialize_job(fresh)), 200
                row = db.execute(
                    "SELECT * FROM jobs WHERE id = ? AND owner = ?",
                    (job_id, session["username"]),
                ).fetchone()
                status = row["status"] if row else None
                if status in ("succeeded", "partial_success", "failed", "cancelled", "cancelling"):
                    return fail("cancel_rejected", "这个任务已经不能取消。", 409)
            updated = db.execute(
                "UPDATE jobs SET cancel_requested = 1, cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND owner = ? AND status = 'running'",
                (timestamp, timestamp, job_id, session["username"]),
            )
            if updated.rowcount != 1:
                return fail("cancel_rejected", "这个任务已经不能取消。", 409)
            fresh = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        try:
            from web.cancel import request_cancel

            request_cancel(job_id)
        except Exception:
            current_app.logger.exception("RQ cancel failed for %s", job_id)
        return jsonify(job=serialize_job(fresh)), 202

    @bp.get("/jobs/<job_id>", endpoint="job_page")
    @login_required
    def job_page(job_id):
        row = owned_job(job_id)
        step_view = job_step_view(row)
        return render_template(
            "job.html",
            username=session["username"],
            csrf_token=csrf_token(),
            job=serialize_job(row),
            work_items=step_view["work_items"],
            progress=step_view["progress"],
            detail=job_detail(connection, row, step_view),
        )

    @bp.get("/api/jobs/<job_id>", endpoint="job_status")
    @login_required
    def job_status(job_id):
        row = owned_job(job_id)
        job = serialize_job(row)
        step_view = job_step_view(row)
        job["work_items"] = step_view["work_items"]
        job["progress"] = step_view["progress"]
        job["detail"] = job_detail(connection, row, step_view)
        return jsonify(job=job)

    @bp.get("/jobs/<job_id>/fastp.html", endpoint="fastp_html")
    @login_required
    def fastp_html(job_id):
        row = owned_job(job_id)
        path = fastp_html_file(pipeline_directories(row["output_dir"]))
        if path is None:
            abort(404)
        response = send_file(path, mimetype="text/html; charset=utf-8", as_attachment=False)
        response.headers["X-Fastp-Report"] = "1"
        response.headers["Content-Disposition"] = "inline"
        return response

    @bp.get("/jobs/<job_id>/report", endpoint="job_report")
    @login_required
    def job_report(job_id):
        row = owned_job(job_id)
        report = job_report_model(connection, row)
        if report is None:
            abort(404, description="这份任务还没有可导出的结果。")
        return render_template(
            "report.html",
            username=session["username"],
            csrf_token=csrf_token(),
            job=serialize_job(row),
            report=report,
            fastp_html=fastp_html_file(pipeline_directories(row["output_dir"])) is not None,
        )

    @bp.get("/api/jobs/<job_id>/reports/<kind>", endpoint="download_job_report")
    @login_required
    def download_job_report(job_id, kind):
        if kind not in ("html", "tsv"):
            abort(404)
        row = owned_job(job_id)
        report = job_report_model(connection, row)
        if report is None:
            abort(404, description="这份任务还没有可导出的结果。")
        html = render_template(
            "report_document.html",
            report=report,
            job=serialize_job(row),
            fastp_html=fastp_html_file(pipeline_directories(row["output_dir"])) is not None,
        )
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

    @bp.get("/api/jobs/<job_id>/log", endpoint="job_log")
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

    @bp.get("/api/jobs/<job_id>/log/download", endpoint="download_job_log")
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

    @bp.get("/api/jobs/<job_id>/download", endpoint="download_job")
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

    @bp.get("/api/jobs/<job_id>/files", endpoint="download_job_file")
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

    @bp.post("/api/jobs/<job_id>/rerun", endpoint="rerun_job")
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
        job_dir = current_app.config["JOBS_ROOT"] / new_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        log_path = job_dir / "logs" / "job.log"
        try:
            threads = read_runtime_limits()["method_threads"]
        except (OSError, RuntimeError, ValueError):
            current_app.logger.exception("Could not read CPU allocation")
            return rerun_response({"error": RESOURCE_START}, 503)
        try:
            for directory in (input_dir, job_dir / "work", output_dir, log_path.parent):
                directory.mkdir(parents=True)
            input_r1, input_r2 = copy_job_inputs(row, input_dir)
            pair_log = current_app.config["TEMP_ROOT"] / "fastq-checks" / str(uuid.uuid4())
            validate_read_pair_for_job(input_r1, input_r2, log_dir=pair_log)
            for name in ("fastq_pair.log", "fastq_pair.json"):
                source = pair_log / name
                if source.is_file():
                    relocate_file(source, log_path.parent / name)
            log_path.write_text(
                "MODULE: {}\nSTATUS: queued\nRERUN OF: {}\n".format(row["module_id"], row["id"]),
                encoding="utf-8",
            )
            with connection() as db:
                insert_rerun_job_catalog(
                    db,
                    owner=session["username"],
                    source_row=row,
                    new_id=new_id,
                    input_r1=input_r1,
                    input_r2=input_r2,
                    output_dir=output_dir,
                    log_path=log_path,
                )
        except FileNotFoundError:
            shutil.rmtree(job_dir, ignore_errors=True)
            return rerun_response({"error": "原来的测序文件已经不在，不能重新分析。"}, 409)
        except OSError:
            shutil.rmtree(job_dir, ignore_errors=True)
            current_app.logger.exception("Could not copy reads for a new analysis")
            return rerun_response({"error": "新任务没有保存成功，请检查磁盘空间。"}, 507)
        except sqlite3.Error:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        try:
            enqueue_analysis_task(
                job_id=new_id,
                owner=session["username"],
                sample=row["sample"],
                assemblers=assemblers,
                threads=threads,
                spades_kmers=row["spades_kmers"] or None,
                spades_careful=bool(row["spades_careful"]),
                input_r1=str(input_r1),
                input_r2=str(input_r2),
                output_dir=str(output_dir),
                log_path=str(log_path),
            )
        except Exception:
            current_app.logger.exception("Could not enqueue RQ pipeline for rerun %s", new_id)
            return rerun_response(
                {"error": "无法启动分析队列，请稍后重试。"},
                503,
            )
        return rerun_response(
            {"job": {"id": new_id, "status": "queued", "rerun_of": row["id"]}},
            202,
        )