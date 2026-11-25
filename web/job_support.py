"""Job query helpers and thin re-exports for Flask wiring."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from flask import abort, current_app

from web.host_monitor import format_bytes, snapshot
from web.job_paths import pipeline_root_candidates
from web.report_io import send_regular_report, store_report_files, write_regular_file
from web.reports import THRESHOLD_ABSENT, scope_comparison
from web.storage_paths import contained_path, is_inside
from web.upload_io import (
    FASTQ_SUFFIXES,
    assemble_upload,
    chunk_byte_length,
    fastq_extension,
    parse_byte_size,
    received_indexes,
    serialize_upload,
    session_directory,
    valid_fastq_filename,
    validated_fastq_name,
    write_exact_stream,
)

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
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


def filtered_comparison(
    directories: list[Path],
    assemblers: list[str] | None,
    evaluation: str,
    work_items: list[dict] | None = None,
):
    """Compare QUAST scopes from on-disk reports; absent via succeeded 工作项 without report."""
    succeeded = {
        item["name"]
        for item in work_items or []
        if item.get("status") == "succeeded" and isinstance(item.get("name"), str)
    }
    rows = []
    absent = {}
    folder = QUAST_SCOPE_DIRS[evaluation]
    for name in assemblers or []:
        if name not in ASSEMBLERS:
            continue
        text = first_pipeline_text(
            directories,
            "quast/{}/{}/report.tsv".format(name, folder),
            2 * 1024 * 1024,
        )
        parsed = report_metric_row(name, text)
        if parsed is not None:
            rows.append(parsed)
            continue
        quast_id = "{}:quast:{}".format(name, evaluation)
        if quast_id in succeeded:
            absent[name] = THRESHOLD_ABSENT
    return rows, absent


def pipeline_comparisons(
    output_dir: str,
    assemblers: list[str] | None,
    original_rows: list[dict],
    directories: list[Path],
    work_items: list[dict] | None = None,
):
    views = [scope_comparison("original", assemblers, original_rows)]
    for evaluation in ("gt500", "gt1000"):
        rows, absent = filtered_comparison(
            directories, assemblers, evaluation, work_items=work_items
        )
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


def recorded_steps(row: sqlite3.Row) -> list[dict]:
    from web.step_read_model import job_step_view

    return job_step_view(row)["work_items"]


def stage_failures(row: sqlite3.Row) -> list[dict]:
    from web.step_read_model import job_step_view

    return job_step_view(row)["failures"]


def job_progress(row: sqlite3.Row) -> dict:
    from web.step_read_model import job_step_view

    return job_step_view(row)["progress"]


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
