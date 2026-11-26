"""Catalog, history, and module overview HTTP handlers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    get_flashed_messages,
    jsonify,
    render_template,
    request,
    session,
)

from web.job_artifacts import (
    align_comparison,
    comparison_file,
    inputs_ready,
    notable_output_files,
    parse_comparison_tsv,
)
from web.job_serialize import (
    ASSEMBLERS,
    FINISHED_STATUSES,
    JOB_STATUSES,
    JOB_STATUS_LABELS,
    data_file,
    elapsed_seconds,
    format_duration,
    host_view,
    parse_filter_date,
    parse_positive_int,
    serialize_job,
)
from web.modules import group_enabled_modules


def register_routes(bp: Blueprint, hooks) -> None:
    login_required = hooks.login_required
    csrf_token = hooks.csrf_token
    connection = hooks.connection
    owned_job = hooks.owned_job

    def overview_jobs(owner: str) -> dict:
        modules = {module["id"]: module for module in current_app.config["ANALYSIS_MODULES"]}
        with connection() as db:
            count_rows = db.execute(
                "SELECT status, COUNT(*) AS total FROM jobs WHERE owner = ? GROUP BY status",
                (owner,),
            ).fetchall()
            recent_rows = db.execute(
                "SELECT * FROM jobs WHERE owner = ? ORDER BY sequence DESC LIMIT 6",
                (owner,),
            ).fetchall()
            report_rows = db.execute(
                "SELECT * FROM jobs WHERE owner = ? AND archive_path IS NOT NULL "
                "ORDER BY sequence DESC LIMIT 5",
                (owner,),
            ).fetchall()
        counts = {status: 0 for status in JOB_STATUSES}
        for row in count_rows:
            if row["status"] in counts:
                counts[row["status"]] = row["total"]

        def present(row: sqlite3.Row) -> dict:
            job = serialize_job(row)
            module = modules.get(job["module_id"], {})
            job["module_name"] = module.get("name", job["module_id"])
            job["module_code"] = module.get("code", "—")
            return job

        reports = []
        for row in report_rows:
            job = present(row)
            if job["archive_available"]:
                reports.append(job)
        return {
            "counts": counts,
            "active_count": counts["queued"] + counts["running"] + counts["cancelling"],
            "total_count": sum(counts.values()),
            "recent_jobs": [present(row) for row in recent_rows],
            "reports": reports,
        }

    def module_usage(owner: str) -> dict:
        with connection() as db:
            rows = db.execute(
                "SELECT module_id, "
                "SUM(status = 'queued') AS queued, "
                "SUM(status = 'running') AS running, "
                "SUM(status = 'cancelling') AS cancelling, "
                "MAX(created_at) AS last_used_at "
                "FROM jobs WHERE owner = ? GROUP BY module_id",
                (owner,),
            ).fetchall()
        return {
            row["module_id"]: {
                "queued": row["queued"],
                "running": row["running"],
                "cancelling": row["cancelling"],
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        }

    def owned_sample_names(owner: str) -> list[str]:
        with connection() as db:
            rows = db.execute(
                "SELECT name FROM samples WHERE owner = ? ORDER BY name",
                (owner,),
            ).fetchall()
        return [row["name"] for row in rows]

    def history_filters() -> dict:
        sample = request.args.get("sample", "")
        if "\x00" in sample:
            abort(400, description="样本名称不正确。")
        module_id = request.args.get("module", "")
        known_modules = {module["id"] for module in current_app.config["ANALYSIS_MODULES"]}
        if module_id and module_id not in known_modules:
            abort(400, description="没有这个分析模块。")
        status = request.args.get("status", "")
        if status and status not in JOB_STATUSES:
            abort(400, description="没有这个任务状态。")
        start = parse_filter_date(request.args.get("start", ""))
        end = parse_filter_date(request.args.get("end", ""))
        if start and end and start > end:
            abort(400, description="日期范围不正确。")
        return {
            "sample": sample,
            "module": module_id,
            "status": status,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "start_date": start,
            "end_date": end,
        }

    def present_history_job(row, modules, current_time) -> dict:
        job = serialize_job(row)
        module = modules.get(job["module_id"], {})
        job["module_name"] = module.get("name", job["module_id"])
        job["module_code"] = module.get("code", "—")
        job["log_available"] = data_file(row["log_path"]) is not None
        job["result_available"] = data_file(row["archive_path"]) is not None
        job["inputs_available"] = inputs_ready(row)
        job["result_files"] = notable_output_files(row["output_dir"])
        job["runtime_seconds"] = elapsed_seconds(
            job["started_at"], job["completed_at"], current_time
        )
        job["runtime_label"] = (
            "等待开始" if job["runtime_seconds"] is None else format_duration(job["runtime_seconds"])
        )
        return job

    @bp.get("/overview", endpoint="overview")
    @login_required
    def overview():
        return render_template(
            "overview.html",
            username=session["username"],
            csrf_token=csrf_token(),
            modules=current_app.config["ANALYSIS_MODULES"],
            host=host_view(current_app.config["DATA_ROOT"]),
            **overview_jobs(session["username"]),
        )

    @bp.get("/api/host", endpoint="host_status")
    @login_required
    def host_status():
        return jsonify(host=host_view(current_app.config["DATA_ROOT"]))

    @bp.get("/modules", endpoint="module_index")
    @login_required
    def module_index():
        modules = current_app.config["ANALYSIS_MODULES"]
        return render_template(
            "modules.html",
            username=session["username"],
            csrf_token=csrf_token(),
            modules=modules,
            categories=group_enabled_modules(modules, module_usage(session["username"])),
        )

    @bp.get("/", endpoint="dashboard")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            username=session["username"],
            csrf_token=csrf_token(),
            assemblers=ASSEMBLERS,
            samples=owned_sample_names(session["username"]),
        )

    @bp.get("/history", endpoint="job_history")
    @login_required
    def job_history():
        page = parse_positive_int(request.args.get("page", "1"))
        if page is None:
            abort(400, description="page must be a positive integer")
        filters = history_filters()
        page_size = 50
        owner = session["username"]
        clauses = ["owner = ?"]
        params: list = [owner]
        if filters["sample"]:
            clauses.append("instr(sample, ?) > 0")
            params.append(filters["sample"])
        if filters["module"]:
            clauses.append("module_id = ?")
            params.append(filters["module"])
        if filters["status"]:
            clauses.append("status = ?")
            params.append(filters["status"])
        if filters["start_date"]:
            clauses.append("created_at >= ?")
            params.append(filters["start"])
        if filters["end_date"]:
            clauses.append("created_at < ?")
            params.append((filters["end_date"] + timedelta(days=1)).isoformat())
        where = " AND ".join(clauses)
        with connection() as db:
            total_count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE {}".format(where), tuple(params)
            ).fetchone()[0]
            total_pages = max(1, (total_count + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = db.execute(
                "SELECT * FROM jobs WHERE {} ORDER BY sequence DESC LIMIT ? OFFSET ?".format(where),
                tuple(params + [page_size, (page - 1) * page_size]),
            ).fetchall()
            comparable_rows = db.execute(
                "SELECT id, sample, status, output_dir, created_at FROM jobs "
                "WHERE owner = ? AND status IN ('succeeded', 'partial_success', 'failed', 'cancelled') "
                "ORDER BY sequence DESC",
                (owner,),
            ).fetchall()

        modules = {module["id"]: module for module in current_app.config["ANALYSIS_MODULES"]}
        current_time = datetime.now(timezone.utc)
        history_jobs = [present_history_job(row, modules, current_time) for row in rows]
        comparable_jobs = [
            {
                "id": row["id"],
                "sample": row["sample"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in comparable_rows
            if comparison_file(row["output_dir"]) is not None
        ]
        filtered = any(filters[key] for key in ("sample", "module", "status", "start", "end"))
        view_filters = {key: filters[key] for key in ("sample", "module", "status", "start", "end")}
        return render_template(
            "history.html",
            username=session["username"],
            csrf_token=csrf_token(),
            jobs=history_jobs,
            page=page,
            total_pages=total_pages,
            total_count=total_count,
            filters=view_filters,
            filtered=filtered,
            status_choices=JOB_STATUSES,
            status_labels=JOB_STATUS_LABELS,
            comparable_jobs=comparable_jobs,
            messages=get_flashed_messages(),
        )

    @bp.get("/history/compare", endpoint="compare_jobs")
    @login_required
    def compare_jobs():
        left_id = request.args.get("left", "")
        right_id = request.args.get("right", "")
        if not left_id or not right_id or left_id == right_id:
            abort(400, description="请选择两个不同的任务。")
        left = owned_job(left_id)
        right = owned_job(right_id)
        summaries = []
        for row in (left, right):
            if row["status"] not in FINISHED_STATUSES:
                abort(400, description="只有已结束的任务可以比较。")
            summary_path = comparison_file(row["output_dir"])
            summary = parse_comparison_tsv(summary_path) if summary_path else []
            if not summary:
                abort(400, description="这条任务没有比较表，不能比较。")
            summaries.append(summary)
        return render_template(
            "compare.html",
            username=session["username"],
            csrf_token=csrf_token(),
            left=serialize_job(left),
            right=serialize_job(right),
            rows=align_comparison(summaries[0], summaries[1]),
            status_labels=JOB_STATUS_LABELS,
        )

    @bp.get("/api/jobs", endpoint="list_jobs")
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