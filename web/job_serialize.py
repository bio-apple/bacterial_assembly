"""Job row serialization and list/present helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, current_app

from web.host_monitor import format_bytes, snapshot
from web.storage_paths import contained_path

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
FINISHED_STATUSES = ("succeeded", "partial_success", "failed", "cancelled")
JOB_STATUSES = (
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "partial_success",
    "failed",
)
JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "分析中",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "succeeded": "已完成",
    "partial_success": "部分完成",
    "failed": "失败",
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


def parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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