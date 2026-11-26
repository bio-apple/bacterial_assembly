"""Persist each planned pipeline step without letting the browser write it."""

from scripts.pipeline_plan import planned_stages

STEP_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "skipped")
FILE_STATUS = {
    "waiting": "queued",
    "running": "running",
    "done": "succeeded",
    "failed": "failed",
}
TERMINAL_JOBS = ("succeeded", "partial_success", "failed", "cancelled")
TERMINAL_STEPS = ("succeeded", "failed", "cancelled", "skipped")


def clean_step_error(value: str) -> str:
    kept = []
    for char in value:
        if char == "\n" or char >= " ":
            kept.append(char)
    return "".join(kept).strip()[:500]


def _exit_code(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamp(value):
    if isinstance(value, str) and value.strip():
        return value.strip()[:40]
    return None


def step_status(file_state: str | None, job_status: str) -> str:
    status = FILE_STATUS.get(file_state or "", "queued")
    if job_status == "cancelled" and status in ("queued", "running"):
        return "cancelled"
    if job_status in TERMINAL_JOBS and status == "queued":
        return "skipped"
    if job_status == "partial_success" and status == "running":
        return "failed"
    if job_status in ("failed", "cancelled") and status == "running":
        return "failed" if job_status == "failed" else "cancelled"
    return status


def sync_job_steps(db, job_id: str, assemblers: list[str], record: dict, job_status: str) -> None:
    if any(not isinstance(name, str) for name in assemblers):
        return
    try:
        planned = planned_stages(assemblers)
    except ValueError:
        return
    recorded = {}
    for stage in record.get("stages") or []:
        if isinstance(stage, dict) and isinstance(stage.get("id"), str):
            recorded[stage["id"]] = stage
    if not recorded:
        return
    for stage in planned:
        overlay = recorded.get(stage["id"]) or {}
        file_state = overlay.get("state") if isinstance(overlay.get("state"), str) else None
        status = step_status(file_state, job_status)
        if status not in STEP_STATUSES:
            continue
        started = _timestamp(overlay.get("started_at"))
        completed = _timestamp(overlay.get("finished_at")) if status in TERMINAL_STEPS else None
        exit_code = _exit_code(overlay.get("exit_code"))
        error = None
        if status == "failed":
            detail = overlay.get("detail")
            error = clean_step_error(detail) if isinstance(detail, str) else ""
            if not error:
                error = "这一步失败了，日志里没有写出原因。"
        db.execute(
            "INSERT INTO job_steps (job_id, step_name, status, started_at, completed_at, exit_code, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, step_name) DO UPDATE SET "
            "status = excluded.status, "
            "started_at = COALESCE(job_steps.started_at, excluded.started_at), "
            "completed_at = excluded.completed_at, "
            "exit_code = COALESCE(excluded.exit_code, job_steps.exit_code), "
            "error = excluded.error "
            "WHERE job_steps.status IN ('queued', 'running') "
            "OR excluded.status IN ('succeeded', 'failed', 'cancelled', 'skipped')",
            (job_id, stage["id"], status, started, completed, exit_code, error),
        )


def step_rows(db, job_id: str, assemblers: list[str]) -> list[dict]:
    try:
        planned = planned_stages(assemblers)
    except ValueError:
        return []
    labels = {stage["id"]: stage["summary"] for stage in planned}
    found = db.execute(
        "SELECT step_name, status, started_at, completed_at, exit_code, error "
        "FROM job_steps WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    rows = []
    for row in found:
        name = row["step_name"]
        if name not in labels or row["status"] not in STEP_STATUSES:
            continue
        rows.append(
            {
                "name": name,
                "label": labels[name],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "exit_code": row["exit_code"],
                "error": row["error"],
            }
        )
    return rows
