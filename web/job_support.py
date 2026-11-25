"""Upload, job query, and report helpers used by the Flask wiring module."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from flask import abort, current_app, send_file
from werkzeug.utils import secure_filename

from scripts.stage_record import build_progress, load_stage_record
from web.host_monitor import format_bytes, snapshot
from web.job_paths import pipeline_root_candidates, stage_record_path
from web.reports import THRESHOLD_ABSENT, scope_comparison
from web.steps import step_rows
from web.storage_paths import contained_path, is_inside

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
RESULT_FILENAMES = {"comparison.tsv"}
RESULT_SUFFIXES = (".fasta", ".fa", ".fna", ".fas")
FASTP_JSON_LIMIT = 20 * 1024 * 1024
QUAST_SCOPE_DIRS = {"gt500": "gt500bp", "gt1000": "gt1000bp"}
FINISHED_STATUSES = ("succeeded", "partial_success", "failed", "cancelled")
JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "分析中",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "succeeded": "已完成",
    "partial_success": "部分完成",
    "failed": "失败",
    "skipped": "未执行",
}


def column_value(row: sqlite3.Row, name: str):
    return row[name] if name in row.keys() else None


def data_file(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        path = contained_path(current_app.config["DATA_ROOT"], raw)
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


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




def pipeline_directories(output_dir: str) -> list[Path]:
    try:
        output = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        return []
    found = []
    root = current_app.config["DATA_ROOT"].resolve()
    for candidate in pipeline_root_candidates(output):
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




def pipeline_stages(output_dir: str) -> list[dict]:
    try:
        output = contained_path(current_app.config["DATA_ROOT"], output_dir)
    except (OSError, ValueError):
        return []
    record = load_stage_record(stage_record_path(output))
    return record["stages"]


def report_metric_row(assembler: str, text: str | None) -> dict | None:
    if not text or not text.strip():
        return None
    metrics = {}
    for row in csv.reader(text.splitlines(), delimiter="\t"):
        if len(row) > 1 and row[0].strip():
            metrics[row[0].strip()] = row[1].strip()
    if not metrics:
        return None
    return {"assembler": assembler, "metrics": metrics}


def filtered_comparison(directories: list[Path], assemblers: list[str] | None, evaluation: str, stages: list[dict]):
    rows = []
    absent = {}
    folder = QUAST_SCOPE_DIRS[evaluation]
    stage_id_suffix = ":quast:{}".format(evaluation)
    skipped = {
        stage["id"][: -len(stage_id_suffix)]
        for stage in stages
        if stage.get("id", "").endswith(stage_id_suffix)
        and stage.get("state") == "done"
        and stage.get("detail") == THRESHOLD_ABSENT
    }
    for name in assemblers or []:
        if name not in ASSEMBLERS:
            continue
        parsed = report_metric_row(
            name,
            first_pipeline_text(
                directories,
                "quast/{}/{}/report.tsv".format(name, folder),
                2 * 1024 * 1024,
            ),
        )
        if parsed is not None:
            rows.append(parsed)
        elif name in skipped:
            absent[name] = THRESHOLD_ABSENT
    return rows, absent


def pipeline_comparisons(output_dir: str, assemblers: list[str] | None, original_rows: list[dict], directories: list[Path]):
    stages = pipeline_stages(output_dir)
    views = [scope_comparison("original", assemblers, original_rows)]
    for evaluation in ("gt500", "gt1000"):
        rows, absent = filtered_comparison(directories, assemblers, evaluation, stages)
        views.append(
            scope_comparison(
                evaluation,
                assemblers,
                rows,
                absent_text=absent,
                waiting="这一口径还没有评估结果。",
            )
        )
    return views


def first_pipeline_text(directories: list[Path], relative: str, limit: int) -> str | None:
    for directory in directories:
        text = read_regular_text(directory, relative, limit)
        if text and text.strip():
            return text
    return None


def fastp_html_file(directories: list[Path]) -> Path | None:
    found = []
    for directory in directories:
        folder = directory / "fastp"
        try:
            if folder.is_symlink() or not folder.is_dir() or not is_inside(directory, folder):
                continue
            names = sorted(path.name for path in folder.iterdir())
        except OSError:
            continue
        for name in names:
            if not name.endswith("_fastp.html"):
                continue
            path = folder / name
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not is_inside(folder, path)
                    or path.stat().st_size < 1
                ):
                    continue
            except OSError:
                continue
            found.append(path)
    return found[0] if found else None


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


def recorded_steps(row: sqlite3.Row) -> list[dict]:
    try:
        assemblers = json.loads(row["assemblers"])
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(assemblers, list) or any(not isinstance(item, str) for item in assemblers):
        return []
    try:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        try:
            rows = step_rows(db, row["id"], assemblers)
        finally:
            db.close()
    except sqlite3.Error:
        current_app.logger.exception("Could not load job steps")
        return []
    return rows


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
