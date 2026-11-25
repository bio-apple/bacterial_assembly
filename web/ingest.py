"""测序文件进入可分析状态：记录校验与配对检查编排。"""

from __future__ import annotations

from pathlib import Path

from web.fastq_check import check_fastq_records, check_read_pair


def validate_fastq_for_analysis(
    path: Path,
    check_command=None,
    *,
    log_dir: Path,
) -> None:
    """字节已齐的单文件：记录校验通过后即为可分析（尚须配对才能建任务）。"""
    check_fastq_records(path, check_command, log_dir=log_dir)


def validate_read_pair_for_job(
    r1: Path,
    r2: Path,
    *,
    log_dir: Path,
) -> None:
    check_read_pair(r1, r2, log_dir)
