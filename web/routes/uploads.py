"""Chunked FASTQ upload HTTP handlers."""

from __future__ import annotations

import hmac
import re
import shutil
import sqlite3
import uuid

from flask import Blueprint, current_app, jsonify, request, session

from web.db_sqlite import relocate_file
from web.errors import FASTQ_CHECK_UNAVAILABLE, FASTQ_EMPTY, FASTQ_RECORDS, parameter_message
from web.fastq_check import FastqCheckUnavailable, FastqRecordError
from web.ingest import validate_fastq_for_analysis
from web.job_support import (
    assemble_upload,
    chunk_byte_length,
    fastq_extension,
    parse_byte_size,
    received_indexes,
    serialize_upload,
    session_directory,
    validated_fastq_name,
    write_exact_stream,
)
from web.storage_paths import is_inside


def register_routes(bp: Blueprint, hooks) -> None:
    login_required = hooks.login_required
    require_csrf = hooks.require_csrf
    fail = hooks.fail
    storage_error = hooks.storage_error
    upload_value_error = hooks.upload_value_error
    connection = hooks.connection
    load_owned_upload = hooks.load_owned_upload
    upload_payload = hooks.upload_payload
    path_fields = hooks.PATH_FIELDS

    @bp.post("/api/uploads", endpoint="create_upload")
    @login_required
    def create_upload():
        require_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return fail("parameter_invalid", parameter_message("上传参数不正确。"), 400)
        if path_fields.intersection(payload) or path_fields.intersection(request.args):
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
        chunk_size = current_app.config["UPLOAD_CHUNK_SIZE"]
        chunk_count = (size + chunk_size - 1) // chunk_size
        upload_id = str(uuid.uuid4())
        try:
            directory = session_directory(current_app.config["STAGING_ROOT"], upload_id)
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

    @bp.get("/api/uploads/<upload_id>", endpoint="upload_status")
    @login_required
    def upload_status(upload_id):
        row = load_owned_upload(upload_id)
        if row is None:
            return fail("not_found", "找不到对应的上传。", 404)
        return jsonify(upload=upload_payload(row))

    @bp.put("/api/uploads/<upload_id>/chunks/<int:index>", endpoint="upload_chunk")
    @login_required
    def upload_chunk(upload_id, index):
        require_csrf()
        if path_fields.intersection(request.args):
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
            chunk_dir = session_directory(current_app.config["STAGING_ROOT"], row["id"]) / "chunks"
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
        received = received_indexes(current_app.config["STAGING_ROOT"], row)
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

    @bp.post("/api/uploads/<upload_id>/complete", endpoint="complete_upload")
    @login_required
    def complete_upload(upload_id):
        require_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return fail("parameter_invalid", parameter_message("上传参数不正确。"), 400)
        if path_fields.intersection(payload) or path_fields.intersection(request.args):
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
            directory = session_directory(current_app.config["STAGING_ROOT"], row["id"])
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
            validate_fastq_for_analysis(
                staged,
                current_app.config.get("FASTQ_CHECK_COMMAND"),
                log_dir=current_app.config["TEMP_ROOT"] / "fastq-checks" / row["id"],
            )
        except FastqRecordError as error:
            staged.unlink(missing_ok=True)
            with connection() as db:
                db.execute(
                    "UPDATE uploads SET status = 'failed', sha256 = NULL, assembled_path = NULL, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND owner = ?",
                    (row["id"], session["username"]),
                )
            message = str(error)
            if "记录无法通过检查" not in message:
                message = FASTQ_RECORDS
            return fail("fastq_invalid", message, 400)
        except FastqCheckUnavailable:
            staged.unlink(missing_ok=True)
            current_app.logger.exception("Could not check uploaded FASTQ records")
            return fail("resource_unavailable", FASTQ_CHECK_UNAVAILABLE, 503)
        validated_dir = current_app.config["UPLOAD_VALIDATED"] / directory.name
        destination = validated_dir / staged.name
        try:
            if not is_inside(current_app.config["UPLOAD_VALIDATED"], destination):
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