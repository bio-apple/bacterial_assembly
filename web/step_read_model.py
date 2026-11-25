"""步骤读模型: one interface for 任务页 step rows (ADR 0015 / deepen)."""

from __future__ import annotations

import json
import logging
import sqlite3

from flask import current_app

from scripts.stage_record import planned_stages
from web.pg import connection
from web.steps import STEP_STATUSES, step_rows
from web.task_backend import use_rq_pipeline

logger = logging.getLogger(__name__)


def load_recorded_steps(job_id: str, assemblers: list[str]) -> list[dict]:
    """
    postgres_rq: Postgres is the source of truth.
    sqlite / legacy: SQLite job_steps only (no disk fallback for RQ).
    """
    if not isinstance(assemblers, list) or any(not isinstance(item, str) for item in assemblers):
        return []
    if use_rq_pipeline():
        try:
            return recorded_steps_from_pg(job_id, assemblers)
        except Exception:
            logger.exception("Could not load Postgres job steps for %s", job_id)
            return []
    return _recorded_steps_from_sqlite(job_id, assemblers)


def recorded_steps_from_pg(job_id: str, assemblers: list[str]) -> list[dict]:
    try:
        planned = planned_stages(assemblers)
    except ValueError:
        return []
    labels = {stage["id"]: stage["summary"] for stage in planned}
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
    rows: list[dict] = []
    for stage in planned:
        name = stage["id"]
        source = None
        if name in by_name:
            source = by_name[name]
        elif name == "comparison" and "summary" in by_name:
            source = by_name["summary"]
        elif stage["kind"] == "quast":
            parent = "quast:{}".format(stage["group"])
            source = by_name.get(parent)
        if source is None:
            continue
        status = source["status"]
        if status == "pending":
            status = "queued"
        if status not in STEP_STATUSES:
            continue
        rows.append(
            {
                "name": name,
                "label": labels.get(name, name),
                "status": status,
                "started_at": _ts(source.get("started_at")),
                "completed_at": _ts(source.get("completed_at")),
                "exit_code": source.get("exit_code"),
                "error": source.get("error"),
            }
        )
    return rows


def _recorded_steps_from_sqlite(job_id: str, assemblers: list[str]) -> list[dict]:
    rows: list[dict] = []
    try:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
        db.row_factory = sqlite3.Row
        try:
            rows = step_rows(db, job_id, assemblers)
        finally:
            db.close()
    except sqlite3.Error:
        logger.exception("Could not load job steps from SQLite")
        rows = []
    return rows


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
