"""步骤读模型: 工作项列表、流水线时间线与失败阶段的唯一 Web 读路径。"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from flask import current_app

from scripts.stage_record import planned_stages
from web.pg import connection
from web.steps import STEP_STATUSES, step_status
from web.task_backend import use_rq_pipeline

logger = logging.getLogger(__name__)

_TERMINAL_STEP = frozenset({"succeeded", "failed", "cancelled", "skipped"})


def load_recorded_steps(job_id: str, assemblers: list[str]) -> list[dict]:
    """Ledger rows that already exist in the step store (no planned padding)."""
    if not _valid_assemblers(assemblers):
        return []
    if use_rq_pipeline():
        try:
            return _overlay_from_store(job_id, assemblers, pad_missing=False)
        except Exception:
            logger.exception("Could not load Postgres job steps for %s", job_id)
            return []
    return _overlay_from_sqlite(job_id, assemblers, pad_missing=False)


def load_work_items(job_id: str, assemblers: list[str], job_status: str) -> list[dict]:
    """Full planned skeleton with step statuses (pad missing as queued/skipped)."""
    if not _valid_assemblers(assemblers):
        return []
    if use_rq_pipeline():
        try:
            return _overlay_from_store(job_id, assemblers, pad_missing=True, job_status=job_status)
        except Exception:
            logger.exception("Could not load Postgres work items for %s", job_id)
            return _planned_only(assemblers, job_status)
    return _overlay_from_sqlite(job_id, assemblers, pad_missing=True, job_status=job_status)


def recorded_steps_from_pg(job_id: str, assemblers: list[str]) -> list[dict]:
    return _overlay_from_store(job_id, assemblers, pad_missing=False)


def project_progress(
    work_items: list[dict],
    job_status: str,
    *,
    updated_at: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict:
    """Project timeline groups / current from work_items (step statuses)."""
    stages = [_timeline_stage(item) for item in work_items]
    groups = _group_work_items(work_items)
    current = _select_current(work_items, job_status)
    current_view = {
        "id": None,
        "software": None,
        "label": None,
        "state": None,
        "percent": None,
        "detail": None,
    }
    if current is not None:
        current_view = {
            "id": current["name"],
            "software": current.get("software"),
            "label": current.get("label") or current["name"],
            "state": current["status"],
            "percent": None,
            "detail": current.get("error"),
        }
    stamps = [
        value
        for item in work_items
        for value in (item.get("started_at"), item.get("completed_at"))
        if value
    ]
    progress_updated = max(stamps) if stamps else updated_at
    seconds = _elapsed_seconds(started_at, completed_at)
    return {
        "status": job_status,
        "updated_at": progress_updated,
        "current": current_view,
        "stages": stages,
        "groups": groups,
        "elapsed_seconds": seconds,
        "elapsed_label": "等待开始" if seconds is None else _format_duration(seconds),
    }


def work_item_failures(work_items: list[dict]) -> list[dict]:
    failures = []
    for item in work_items:
        if item.get("status") != "failed":
            continue
        entry = {"id": item["name"], "label": item.get("label") or item["name"]}
        error = item.get("error")
        if isinstance(error, str) and error.strip():
            entry["detail"] = error.strip()
        failures.append(entry)
    return failures


def job_step_view(row: sqlite3.Row) -> dict:
    """Single seam for 分析任务页: work_items + projected progress + failures."""
    try:
        assemblers = json.loads(row["assemblers"])
    except (TypeError, json.JSONDecodeError):
        assemblers = []
    if not isinstance(assemblers, list):
        assemblers = []
    status = row["status"]
    if use_rq_pipeline() and status == "queued":
        status = _pg_job_status(row["id"]) or status
    work_items = load_work_items(row["id"], assemblers, status)
    progress = project_progress(
        work_items,
        status,
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )
    return {
        "work_items": work_items,
        "progress": progress,
        "failures": work_item_failures(work_items),
    }


def _valid_assemblers(assemblers: list[str]) -> bool:
    return isinstance(assemblers, list) and all(isinstance(item, str) for item in assemblers)


def _planned_meta(assemblers: list[str]) -> list[dict]:
    try:
        return planned_stages(assemblers)
    except ValueError:
        return []


def _planned_only(assemblers: list[str], job_status: str) -> list[dict]:
    return [
        _work_item_from_plan(stage, step_status("waiting", job_status), None)
        for stage in _planned_meta(assemblers)
    ]


def _work_item_from_plan(stage: dict, status: str, source: dict | None) -> dict:
    started = _ts(source.get("started_at")) if source else None
    completed = None
    if source and status in _TERMINAL_STEP:
        completed = _ts(source.get("completed_at"))
    return {
        "name": stage["id"],
        "label": stage["summary"],
        "status": status,
        "started_at": started,
        "completed_at": completed,
        "exit_code": source.get("exit_code") if source else None,
        "error": source.get("error") if source else None,
        "software": stage.get("software"),
        "group": stage["group"],
        "group_title": stage["group_title"],
        "kind": stage.get("kind"),
        "step_label": stage.get("label"),
    }


def _overlay_from_store(
    job_id: str,
    assemblers: list[str],
    *,
    pad_missing: bool,
    job_status: str = "queued",
) -> list[dict]:
    planned = _planned_meta(assemblers)
    if not planned:
        return []
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT step_name, step_type, status, started_at, completed_at, exit_code, error
                FROM job_steps
                WHERE job_id = %s
                ORDER BY created_at ASC
                """,
                (job_id,),
            )
            pg_rows = cur.fetchall()
    by_name = {row["step_name"]: row for row in pg_rows}
    return _merge_planned(planned, by_name, pad_missing=pad_missing, job_status=job_status)


def _overlay_from_sqlite(
    job_id: str,
    assemblers: list[str],
    *,
    pad_missing: bool,
    job_status: str = "queued",
) -> list[dict]:
    planned = _planned_meta(assemblers)
    if not planned:
        return []
    by_name: dict = {}
    try:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        try:
            found = db.execute(
                "SELECT step_name, status, started_at, completed_at, exit_code, error "
                "FROM job_steps WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
            by_name = {row["step_name"]: dict(row) for row in found}
        finally:
            db.close()
    except sqlite3.Error:
        logger.exception("Could not load job steps from SQLite")
        by_name = {}
    return _merge_planned(planned, by_name, pad_missing=pad_missing, job_status=job_status)


def _resolve_source(stage: dict, by_name: dict):
    name = stage["id"]
    if name in by_name:
        return by_name[name]
    if name == "comparison" and "summary" in by_name:
        return by_name["summary"]
    if stage.get("kind") == "quast":
        parent = "quast:{}".format(stage["group"])
        return by_name.get(parent)
    return None


def _merge_planned(
    planned: list[dict],
    by_name: dict,
    *,
    pad_missing: bool,
    job_status: str,
) -> list[dict]:
    rows: list[dict] = []
    for stage in planned:
        source = _resolve_source(stage, by_name)
        if source is None:
            if not pad_missing:
                continue
            status = step_status("waiting", job_status)
            rows.append(_work_item_from_plan(stage, status, None))
            continue
        status = source["status"]
        if status == "pending":
            status = "queued"
        if status not in STEP_STATUSES:
            if not pad_missing:
                continue
            status = step_status("waiting", job_status)
            rows.append(_work_item_from_plan(stage, status, None))
            continue
        rows.append(_work_item_from_plan(stage, status, source))
    return rows


def _timeline_stage(item: dict) -> dict:
    return {
        "id": item["name"],
        "group": item.get("group") or item["name"],
        "group_title": item.get("group_title") or item.get("label") or item["name"],
        "software": item.get("software"),
        "label": item.get("step_label") or item.get("label"),
        "summary": item.get("label") or item["name"],
        "kind": item.get("kind"),
        "state": item["status"],
        "started_at": item.get("started_at"),
        "finished_at": item.get("completed_at"),
        "updated_at": item.get("completed_at") or item.get("started_at"),
        "percent": None,
        "detail": item.get("error"),
    }


def _group_state(statuses: list[str]) -> str:
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if statuses and all(status == "succeeded" for status in statuses):
        return "succeeded"
    if any(status == "succeeded" for status in statuses):
        return "running"
    if statuses and all(status in ("cancelled", "skipped") for status in statuses):
        return "cancelled" if "cancelled" in statuses else "skipped"
    return "queued"


def _group_work_items(work_items: list[dict]) -> list[dict]:
    groups = []
    index = {}
    for item in work_items:
        stage = _timeline_stage(item)
        key = stage["group"]
        if key not in index:
            group = {"id": key, "title": stage["group_title"], "steps": [], "state": "queued"}
            index[key] = group
            groups.append(group)
        index[key]["steps"].append(stage)
    for group in groups:
        group["state"] = _group_state([step["state"] for step in group["steps"]])
    return groups


def _select_current(work_items: list[dict], job_status: str) -> dict | None:
    for item in work_items:
        if item["status"] == "running":
            return item
    if job_status == "queued":
        return None
    for item in reversed(work_items):
        if item["status"] in ("succeeded", "failed"):
            return item
    return None


def _pg_job_status(job_id: str) -> str | None:
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
                pg_job = cur.fetchone()
        if pg_job and pg_job["status"]:
            return pg_job["status"]
    except Exception:
        logger.exception("Could not load Postgres job status for %s", job_id)
    return None


def _ts(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        if text.endswith("+00:00"):
            return text[:-6] + "Z"
        return text
    text = str(value).strip()
    return text or None


def _elapsed_seconds(
    started_at: str | None,
    completed_at: str | None,
) -> int | None:
    if not started_at:
        return None

    def parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    started = parse_timestamp(started_at)
    ended = parse_timestamp(completed_at) if completed_at else datetime.now(timezone.utc)
    return max(0, int((ended - started).total_seconds()))


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{}小时 {}分".format(hours, minutes)
    if minutes:
        return "{}分 {}秒".format(minutes, seconds)
    return "{}秒".format(seconds)
