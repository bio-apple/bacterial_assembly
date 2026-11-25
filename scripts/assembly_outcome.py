"""组装结果声明：assembly_outcome.json 的写入、解析与终态裁决。"""

from __future__ import annotations

import json
from pathlib import Path

OUTCOME_SCHEMA_VERSION = 1
OUTCOME_FILENAME = "assembly_outcome.json"
OUTCOME_TEMP_FILENAME = ".assembly_outcome.json.tmp"

ASSEMBLER_OUTPUTS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}


def write_outcome(
    outdir: Path,
    status: str,
    selected: list[str],
    successful: list[str],
    failed: list[str],
    exit_code: int,
) -> None:
    payload = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "status": status,
        "selected_assemblers": list(selected),
        "successful_assemblers": list(successful),
        "failed_assemblers": list(failed),
        "exit_code": exit_code,
    }
    destination = outdir / OUTCOME_FILENAME
    temporary = outdir / OUTCOME_TEMP_FILENAME
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)


def read_outcome(pipeline_output_dir: Path | None):
    if pipeline_output_dir is None:
        return None
    path = pipeline_output_dir / OUTCOME_FILENAME
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != OUTCOME_SCHEMA_VERSION:
        return None
    if payload.get("status") not in {"succeeded", "partial_success", "failed"}:
        return None
    if type(payload.get("exit_code")) is not int:
        return None
    for key in ("selected_assemblers", "successful_assemblers", "failed_assemblers"):
        value = payload.get(key)
        if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
            return None
    return payload


def assembler_output_present(pipeline_output_dir: Path, assembler: str) -> bool:
    filename = ASSEMBLER_OUTPUTS.get(assembler)
    if filename is None:
        return False
    path = pipeline_output_dir / "assemblies" / assembler / filename
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def finished_status(returncode, pipeline_output_dir: Path | None) -> str:
    payload = read_outcome(pipeline_output_dir)
    if payload is None or returncode != payload["exit_code"] or pipeline_output_dir is None:
        return "failed"
    selected = payload["selected_assemblers"]
    successful = payload["successful_assemblers"]
    failed = payload["failed_assemblers"]
    if set(successful) & set(failed):
        return "failed"
    if not set(successful).issubset(selected) or not set(failed).issubset(selected):
        return "failed"
    if any(not assembler_output_present(pipeline_output_dir, name) for name in successful):
        return "failed"
    status = payload["status"]
    exit_code = payload["exit_code"]
    if status == "succeeded" and exit_code == 0 and successful == selected and not failed:
        return "succeeded"
    if status == "partial_success" and exit_code == 3 and successful and failed:
        return "partial_success"
    return "failed"
