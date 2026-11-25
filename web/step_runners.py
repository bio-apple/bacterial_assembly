"""Execute analysis steps by invoking existing scripts/run_*.py helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.assembly_outcome import write_outcome
from scripts.common import append_log
from scripts.stage_record import QUAST_EVALUATIONS, StageRecord, quast_stage_id
from web.pg import connection

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
ASSEMBLY_CONTIGS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}


def work_dir_for_job(job: dict) -> Path:
    """Pipeline outdir is jobs/<id>/work; jobs.output_dir stores .../output."""
    output = Path(job["output_dir"])
    work = output.parent / "work"
    if work.is_dir() or not output.exists():
        return work
    return output / "outputs"


def _run_script(
    script: str,
    arguments: list[str],
    log_path: Path,
    versions_path: Path,
    thread_count: int,
    job_id: str,
    memory_gib: int | None = None,
) -> None:
    command = [
        sys.executable,
        str(SCRIPTS / script),
        *arguments,
        "--log",
        str(log_path),
        "--versions-file",
        str(versions_path),
    ]
    append_log(log_path, "\n== {} ==\nCOMMAND: {}\n".format(script, " ".join(command)))
    env = os.environ.copy()
    env["_ASSEMBLY_ASSIGNED_THREADS"] = str(thread_count)
    env["_ASSEMBLY_JOB_ID"] = str(job_id)
    if memory_gib is not None:
        env["_ASSEMBLY_METHOD_MEMORY_GIB"] = str(memory_gib)
    # Label docker containers with step when executor reads env.
    env["_ASSEMBLY_STEP_NAME"] = Path(script).stem
    completed = subprocess.run(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.stdout:
        append_log(log_path, completed.stdout)
        print(completed.stdout, end="")
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)


def load_job(job_id: str) -> dict:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
    if job is None:
        raise RuntimeError("unknown job {}".format(job_id))
    return job


def run_fastp_step(job: dict, step: dict) -> None:
    outdir = work_dir_for_job(job)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "logs").mkdir(parents=True, exist_ok=True)
    log_path = outdir / "run.log"
    versions_path = outdir / "versions.txt"
    if not versions_path.exists():
        versions_path.write_text("", encoding="utf-8")
    stages = StageRecord(
        outdir / "stages.json", json.loads(job["assemblers"]), resume=True
    )
    stages.start("fastp")
    try:
        _run_script(
            "run_fastp.py",
            [
                "--r1",
                str(job["input_r1"]),
                "--r2",
                str(job["input_r2"]),
                "--outdir",
                str(outdir / "fastp"),
                "--sample",
                job["sample"],
            ],
            log_path,
            versions_path,
            int(job["threads"]),
            str(job["id"]),
        )
        stages.finish("fastp", exit_code=0)
    except Exception as error:
        stages.finish("fastp", "failed", detail=str(error), exit_code=getattr(error, "returncode", None))
        raise


def run_assembly_step(job: dict, step: dict) -> None:
    method = step["software"] or step["step_name"]
    if method not in ASSEMBLERS:
        raise RuntimeError("unknown assembler {}".format(method))
    outdir = work_dir_for_job(job)
    filtered_r1 = outdir / "fastp" / "{}_R1.fastq.gz".format(job["sample"])
    filtered_r2 = outdir / "fastp" / "{}_R2.fastq.gz".format(job["sample"])
    if not filtered_r1.is_file() or not filtered_r2.is_file():
        raise RuntimeError("clean FASTQ missing; fastp must succeed first")
    method_log = outdir / "logs" / "{}.log".format(method)
    method_versions = outdir / "logs" / "{}.versions.txt".format(method)
    root_log = outdir / "run.log"
    stages = StageRecord(
        outdir / "stages.json", json.loads(job["assemblers"]), resume=True
    )
    stages.start(method)
    arguments = [
        "--r1",
        str(filtered_r1),
        "--r2",
        str(filtered_r2),
        "--outdir",
        str(outdir / "assemblies" / method),
    ]
    if method == "spades":
        if job.get("spades_kmers"):
            arguments += ["--kmers", job["spades_kmers"]]
        if job.get("spades_careful"):
            arguments.append("--careful")
    try:
        _run_script(
            "run_{}.py".format(method),
            arguments,
            method_log,
            method_versions,
            int(job["threads"]),
            str(job["id"]),
        )
        contigs = outdir / "assemblies" / method / ASSEMBLY_CONTIGS[method]
        filtered_500 = outdir / "assemblies" / method / "contigs_gt500bp.fasta"
        filtered_1000 = outdir / "assemblies" / method / "contigs_gt1000bp.fasta"
        stages.note(method, "正在按长度过滤 contig")
        filter_cmd = [
            sys.executable,
            str(SCRIPTS / "filter_contigs.py"),
            "--input",
            str(contigs),
            "--gt500",
            str(filtered_500),
            "--gt1000",
            str(filtered_1000),
        ]
        completed = subprocess.run(
            filter_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.stdout:
            append_log(method_log, completed.stdout)
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, filter_cmd)
        stages.finish(method, exit_code=0)
        append_log(root_log, "[{}] Method assembly complete\n".format(method))
    except Exception as error:
        stages.finish(
            method,
            "failed",
            detail=str(error),
            exit_code=getattr(error, "returncode", None),
        )
        raise


def run_quast_step(job: dict, step: dict) -> None:
    method = (step["software"] or step["step_name"] or "").removeprefix("quast:")
    if method not in ASSEMBLERS:
        # step_name like quast:spades
        name = step["step_name"]
        method = name.split(":", 1)[1] if name.startswith("quast:") else name
    outdir = work_dir_for_job(job)
    contigs = outdir / "assemblies" / method / ASSEMBLY_CONTIGS[method]
    filtered_500 = outdir / "assemblies" / method / "contigs_gt500bp.fasta"
    filtered_1000 = outdir / "assemblies" / method / "contigs_gt1000bp.fasta"
    method_log = outdir / "logs" / "{}.log".format(method)
    method_versions = outdir / "logs" / "{}.versions.txt".format(method)
    stages = StageRecord(
        outdir / "stages.json", json.loads(job["assemblers"]), resume=True
    )
    evaluations = [
        (contigs, outdir / "quast" / method, "original"),
        (filtered_500, outdir / "quast" / method / "gt500bp", ">500 bp"),
        (filtered_1000, outdir / "quast" / method / "gt1000bp", ">1000 bp"),
    ]
    failures = 0
    for (evaluation, quast_dir, label), (evaluation_id, evaluation_label) in zip(
        evaluations, QUAST_EVALUATIONS
    ):
        stage_id = quast_stage_id(method, evaluation_id)
        try:
            if not evaluation.is_file() or evaluation.stat().st_size == 0:
                stages.start(stage_id)
                stages.finish(stage_id, "done", detail="没有达到阈值的 contig", exit_code=0)
                continue
            stages.start(stage_id, detail=evaluation_label)
            _run_script(
                "run_quast.py",
                ["--contigs", str(evaluation), "--outdir", str(quast_dir)],
                method_log,
                method_versions,
                int(job["threads"]),
                str(job["id"]),
            )
            stages.finish(stage_id, exit_code=0)
        except Exception as error:
            failures += 1
            stages.finish(
                stage_id,
                "failed",
                detail=str(error),
                exit_code=getattr(error, "returncode", None),
            )
            append_log(method_log, "QUAST {} failed: {}\n".format(label, error))
    # ADR 0013/0018: mark this quast work item failed without touching assembly success.
    if failures:
        raise RuntimeError(
            "{} QUAST evaluation(s) failed for {}".format(failures, method)
        )


def run_summary_step(job: dict, step: dict) -> None:
    outdir = work_dir_for_job(job)
    selected = json.loads(job["assemblers"])
    successful = []
    failed = []
    with connection() as conn:
        with conn.cursor() as cur:
            for method in selected:
                cur.execute(
                    """
                    SELECT status FROM job_steps
                    WHERE job_id = %s AND step_name = %s
                    """,
                    (job["id"], method),
                )
                row = cur.fetchone()
                status = row["status"] if row else "failed"
                if status == "succeeded":
                    successful.append(method)
                else:
                    failed.append(method)
    root_log = outdir / "run.log"
    if successful:
        command = [
            sys.executable,
            str(SCRIPTS / "summarize_quast.py"),
            "--outdir",
            str(outdir),
        ]
        for name in successful:
            command.extend(["--assembler", name])
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.stdout:
            append_log(root_log, completed.stdout)
        if completed.returncode:
            raise subprocess.CalledProcessError(completed.returncode, command)
    if not successful:
        outcome, exit_code = "failed", 1
    elif failed:
        outcome, exit_code = "partial_success", 3
    else:
        outcome, exit_code = "succeeded", 0
    write_outcome(outdir, outcome, selected, successful, failed, exit_code)
    stages = StageRecord(outdir / "stages.json", selected, resume=True)
    stages.start("comparison")
    stages.finish("comparison", exit_code=0)
