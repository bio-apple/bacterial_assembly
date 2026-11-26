"""Job monitor and report view models."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import ContextManager

from web.job_artifacts import (
    first_pipeline_text,
    input_facts,
    inputs_ready,
    load_fastp_documents,
    parse_comparison_text,
    pipeline_comparisons,
    pipeline_directories,
    fastp_html_file,
)
from web.job_serialize import JOB_STATUS_LABELS, column_value
from web.reports import assemble_report_model, qc_sheet, resource_lines, result_sources_present, selected_assemblers
from web.step_read_model import job_step_view


def linked_sample(connection: Callable[[], ContextManager[sqlite3.Connection]], row: sqlite3.Row) -> tuple[str, dict | None]:
    sample_name = row["sample"]
    sample_id = column_value(row, "sample_id")
    batch_id = column_value(row, "sequencing_batch_id")
    batch = None
    with connection() as db:
        if sample_id:
            found = db.execute(
                "SELECT name FROM samples WHERE id = ? AND owner = ?",
                (sample_id, row["owner"]),
            ).fetchone()
            if found:
                sample_name = found["name"]
        if sample_id and batch_id:
            found_batch = db.execute(
                "SELECT r1_filename, r2_filename, r1_sha256, r2_sha256 "
                "FROM sequencing_batches WHERE id = ? AND owner = ? AND sample_id = ?",
                (batch_id, row["owner"], sample_id),
            ).fetchone()
            if found_batch:
                batch = {
                    "r1_filename": found_batch["r1_filename"],
                    "r2_filename": found_batch["r2_filename"],
                    "r1_sha256": found_batch["r1_sha256"],
                    "r2_sha256": found_batch["r2_sha256"],
                }
    return sample_name, batch


def job_detail(
    connection: Callable[[], ContextManager[sqlite3.Connection]],
    row: sqlite3.Row,
    step_view: dict | None = None,
) -> dict:
    sample_name, batch = linked_sample(connection, row)
    directories = pipeline_directories(row["output_dir"])
    run_log = first_pipeline_text(directories, "run.log", 2 * 1024 * 1024)
    resources = resource_lines(row["threads"], run_log)
    assemblers = selected_assemblers(row["assemblers"])
    comparison_text = first_pipeline_text(directories, "comparison.tsv", 2 * 1024 * 1024)
    parsed = parse_comparison_text(comparison_text)
    view = step_view if step_view is not None else job_step_view(row)
    comparisons = pipeline_comparisons(row["output_dir"], assemblers, parsed, directories)
    return {
        "sample_name": sample_name,
        "sample_id": column_value(row, "sample_id"),
        "sequencing_batch_id": column_value(row, "sequencing_batch_id"),
        "batch": batch,
        "inputs": input_facts(row),
        "parameters": {
            "assemblers": assemblers or [],
            "spades_kmers": row["spades_kmers"],
            "spades_careful": bool(row["spades_careful"]),
        },
        "resources": resources,
        "comparison": comparisons[0],
        "comparisons": comparisons,
        "qc": qc_sheet(load_fastp_documents(directories)),
        "fastp_html": fastp_html_file(directories) is not None,
        "report_ready": result_sources_present(
            first_pipeline_text(directories, "versions.txt", 1024 * 1024),
            load_fastp_documents(directories),
            parsed,
            view["failures"],
        ),
        "inputs_available": inputs_ready(row),
    }


def job_report_model(
    connection: Callable[[], ContextManager[sqlite3.Connection]],
    row: sqlite3.Row,
) -> dict | None:
    sample_name, batch = linked_sample(connection, row)
    directories = pipeline_directories(row["output_dir"])
    versions_text = first_pipeline_text(directories, "versions.txt", 1024 * 1024)
    fastp_documents = load_fastp_documents(directories)
    comparison_rows = parse_comparison_text(
        first_pipeline_text(directories, "comparison.tsv", 2 * 1024 * 1024)
    )
    step_view = job_step_view(row)
    failures = step_view["failures"]
    if not result_sources_present(versions_text, fastp_documents, comparison_rows, failures):
        return None
    assemblers = selected_assemblers(row["assemblers"])
    comparisons = pipeline_comparisons(row["output_dir"], assemblers, comparison_rows, directories)
    resources = resource_lines(
        row["threads"],
        first_pipeline_text(directories, "run.log", 2 * 1024 * 1024),
    )
    return assemble_report_model(
        sample_name=sample_name,
        status_label=JOB_STATUS_LABELS.get(row["status"], row["status"]),
        batch=batch,
        inputs=input_facts(row),
        assemblers=assemblers,
        spades_kmers=row["spades_kmers"],
        spades_careful=bool(row["spades_careful"]),
        resources=resources,
        versions_text=versions_text,
        fastp_documents=fastp_documents,
        comparison_rows=comparison_rows,
        job_error=row["error"],
        stage_failures=failures,
        comparisons=comparisons,
    )