"""Shared catalog insert and enqueue for new analysis and rerun tasks."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from web.modules import BACTERIAL_ASSEMBLY_MODULE_ID
from web.notices import note_job


def discard_stored_files(files: list[Path], directories: list[Path]) -> None:
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def checksum_or_none(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def reuse_or_create_sample(db: sqlite3.Connection, owner: str, name: str) -> str:
    existing = db.execute(
        "SELECT id FROM samples WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()
    if existing:
        return existing[0]
    sample_id = str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO samples (id, owner, name) VALUES (?, ?, ?)",
            (sample_id, owner, name),
        )
    except sqlite3.IntegrityError:
        existing = db.execute(
            "SELECT id FROM samples WHERE owner = ? AND name = ?",
            (owner, name),
        ).fetchone()
        if existing is None:
            raise
        return existing[0]
    return sample_id


def insert_new_job_catalog(
    db: sqlite3.Connection,
    *,
    owner: str,
    sample: str,
    job_id: str,
    assembler_list: list[str],
    threads: int,
    kmers_text: str | None,
    spades_careful: bool,
    input_paths: tuple[Path, Path],
    output_dir: Path,
    log_path: Path,
    original_names: tuple[str, str],
    original_sha: tuple[str | None, str | None],
    claimed: list[sqlite3.Row] | None,
) -> None:
    sample_id = reuse_or_create_sample(db, owner, sample)
    batch_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sequencing_batches (id, owner, sample_id, r1_filename, "
        "r2_filename, r1_sha256, r2_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            owner,
            sample_id,
            original_names[0],
            original_names[1],
            original_sha[0],
            original_sha[1],
        ),
    )
    db.execute(
        "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
        "sample, spades_kmers, spades_careful, input_r1, input_r2, "
        "output_dir, log_path, sample_id, sequencing_batch_id) "
        "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            owner,
            BACTERIAL_ASSEMBLY_MODULE_ID,
            json.dumps(assembler_list),
            threads,
            sample,
            kmers_text or None,
            int(spades_careful),
            str(input_paths[0]),
            str(input_paths[1]),
            str(output_dir),
            str(log_path),
            sample_id,
            batch_id,
        ),
    )
    if claimed:
        for upload, destination in zip(claimed, input_paths):
            cursor = db.execute(
                "UPDATE uploads SET status = 'consumed', assembled_path = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND owner = ? AND status = 'completed'",
                (str(destination), upload["id"], owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("上传尚未完成或校验未通过，不能开始分析。")
    note_job(db, owner, sample, job_id, "submitted")


def insert_rerun_job_catalog(
    db: sqlite3.Connection,
    *,
    owner: str,
    source_row: sqlite3.Row,
    new_id: str,
    input_r1: Path,
    input_r2: Path,
    output_dir: Path,
    log_path: Path,
) -> None:
    from web.job_serialize import column_value

    kept_sample_id = column_value(source_row, "sample_id")
    kept_batch_id = column_value(source_row, "sequencing_batch_id")
    db.execute(
        "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
        "sample, spades_kmers, spades_careful, input_r1, input_r2, "
        "output_dir, log_path, rerun_of, sample_id, sequencing_batch_id) "
        "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            owner,
            source_row["module_id"],
            source_row["assemblers"],
            source_row["threads"],
            source_row["sample"],
            source_row["spades_kmers"],
            int(source_row["spades_careful"]),
            str(input_r1),
            str(input_r2),
            str(output_dir),
            str(log_path),
            source_row["id"],
            kept_sample_id,
            kept_batch_id,
        ),
    )


def enqueue_analysis_task(
    *,
    job_id: str,
    owner: str,
    sample: str,
    assemblers: list[str],
    threads: int,
    spades_kmers: str | None,
    spades_careful: bool,
    input_r1: str,
    input_r2: str,
    output_dir: str,
    log_path: str,
) -> None:
    from web.job_submit import submit_analysis_task

    submit_analysis_task(
        job_id=job_id,
        owner=owner,
        sample=sample,
        assemblers=assemblers,
        threads=threads,
        spades_kmers=spades_kmers,
        spades_careful=spades_careful,
        input_r1=input_r1,
        input_r2=input_r2,
        output_dir=output_dir,
        log_path=log_path,
    )