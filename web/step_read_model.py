"""Web adapter: expose Postgres step state in the SQLite step-row shape."""

from __future__ import annotations

from scripts.stage_record import planned_stages
from web.pg import connection
from web.steps import STEP_STATUSES


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
