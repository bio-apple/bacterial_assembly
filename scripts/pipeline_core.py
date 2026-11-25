"""Shared assembly-method helpers for CLI and RQ step runners."""

from __future__ import annotations

from pathlib import Path

from scripts.assembly_outcome import ASSEMBLER_OUTPUTS

ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
ASSEMBLY_CONTIGS = ASSEMBLER_OUTPUTS


def contig_path(outdir: Path, method: str) -> Path:
    return outdir / "assemblies" / method / ASSEMBLY_CONTIGS[method]


def filtered_contig_paths(outdir: Path, method: str) -> tuple[Path, Path]:
    base = outdir / "assemblies" / method
    return base / "contigs_gt500bp.fasta", base / "contigs_gt1000bp.fasta"


def quast_targets(outdir: Path, method: str) -> list[tuple[Path, Path, str]]:
    contigs = contig_path(outdir, method)
    filtered_500, filtered_1000 = filtered_contig_paths(outdir, method)
    return [
        (contigs, outdir / "quast" / method, "original"),
        (filtered_500, outdir / "quast" / method / "gt500bp", ">500 bp"),
        (filtered_1000, outdir / "quast" / method / "gt1000bp", ">1000 bp"),
    ]


def apply_job_runtime_env(
    env: dict,
    *,
    job_id: str,
    thread_count: int,
    memory_gib: int | None = None,
    step_name: str | None = None,
    step_id: str | None = None,
) -> dict:
    env["_ASSEMBLY_ASSIGNED_THREADS"] = str(thread_count)
    env["_ASSEMBLY_JOB_ID"] = str(job_id)
    if memory_gib is not None:
        env["_ASSEMBLY_METHOD_MEMORY_GIB"] = str(memory_gib)
    if step_name:
        env["_ASSEMBLY_STEP_NAME"] = step_name
    if step_id:
        env["_ASSEMBLY_STEP_ID"] = step_id
    return env
