"""分析任务产物目录：按 ADR 0009 解析 work / output / stages / outcome。"""

from __future__ import annotations

from pathlib import Path

from scripts.stage_record import load_stage_record


def job_work_dir(output_dir: Path) -> Path:
    return output_dir.parent / "work"


def stage_record_candidates(output_dir: Path) -> tuple[Path, ...]:
    return (
        job_work_dir(output_dir) / "stages.json",
        output_dir / "stages.json",
        output_dir / "outputs" / "stages.json",
    )


def stage_record_path(output_dir: Path) -> Path:
    candidates = stage_record_candidates(output_dir)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_job_stage_record(output_dir: Path) -> dict:
    record = {"stages": []}
    for candidate in stage_record_candidates(output_dir):
        loaded = load_stage_record(candidate)
        if loaded.get("stages"):
            return loaded
    return record


def pipeline_root_candidates(output_dir: Path) -> tuple[Path, ...]:
    return (job_work_dir(output_dir), output_dir)
