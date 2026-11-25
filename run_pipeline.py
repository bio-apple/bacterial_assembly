#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from scripts.common import (
    append_log,
    cpu_share,
    logical_cpu_count,
    memory_share_gib,
    physical_memory_bytes,
    prepare_output_dir,
    report_script_error,
    validate_reads,
    validate_sample,
)
from scripts.resource_limits import allocation_log, combine_cpu, combine_memory, read_cgroup
from scripts.run_spades import parse_kmers
from scripts.stage_record import QUAST_EVALUATIONS, StageRecord, quast_stage_id


ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
ASSEMBLY_CONTIGS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Illumina bacterial assembly comparison workflow"
    )
    parser.add_argument("--r1", required=True, help="R1 FASTQ/FASTQ.GZ file")
    parser.add_argument("--r2", required=True, help="R2 FASTQ/FASTQ.GZ file")
    parser.add_argument("--outdir", required=True, help="Empty results directory")
    parser.add_argument("--sample", default="sample")
    parser.add_argument("--job-id", help="Owning analysis job, recorded with the process group")
    parser.add_argument("--spades-kmers", type=parse_kmers)
    parser.add_argument("--spades-careful", action="store_true")
    parser.add_argument(
        "--assembler",
        choices=ASSEMBLERS,
        action="append",
        required=True,
        help="Assembler to run; repeat to select multiple (methods run serially in this order)",
    )
    return parser


def run_script(
    script: str,
    arguments: list[str],
    log_path: Path,
    versions_path: Path,
    thread_count: int | None = None,
    stream_log_path: Path | None = None,
    stream_lock: threading.Lock | None = None,
    log_prefix: str = "",
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
    header = "\n== {} ==\nCOMMAND: {}".format(script, shlex.join(command))
    append_log(log_path, header)
    if stream_log_path:
        with stream_lock:
            append_log(stream_log_path, "{}{}".format(log_prefix, header))
    environment = os.environ.copy()
    if thread_count is not None:
        environment["_ASSEMBLY_ASSIGNED_THREADS"] = str(thread_count)
    if stream_log_path is None:
        subprocess.run(command, check=True, env=environment)
        return

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print("{}{}".format(log_prefix, line), end="", flush=True)
        with stream_lock:
            append_log(stream_log_path, "{}{}".format(log_prefix, line))
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def run_summary(outdir: Path, assemblers: list[str], log_path: Path) -> None:
    command = [
        sys.executable,
        str(SCRIPTS / "summarize_quast.py"),
        "--outdir",
        str(outdir),
    ]
    for assembler in assemblers:
        command.extend(["--assembler", assembler])
    append_log(
        log_path,
        "\n== QUAST comparison summary ==\nCOMMAND: {}".format(shlex.join(command)),
    )
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.stdout:
        print(result.stdout, end="")
        append_log(log_path, result.stdout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)


def run_contig_filter(
    contigs: Path,
    filtered_500: Path,
    filtered_1000: Path,
    log_path: Path,
    stream_log_path: Path | None = None,
    stream_lock: threading.Lock | None = None,
    log_prefix: str = "",
) -> None:
    command = [
        sys.executable,
        str(SCRIPTS / "filter_contigs.py"),
        "--input",
        str(contigs),
        "--gt500",
        str(filtered_500),
        "--gt1000",
        str(filtered_1000),
    ]
    header = "\n== Contig length filtering ==\nCOMMAND: {}".format(shlex.join(command))
    append_log(log_path, header)
    if stream_log_path:
        with stream_lock:
            append_log(stream_log_path, "{}{}".format(log_prefix, header))
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.stdout:
        print(result.stdout, end="")
        append_log(log_path, result.stdout)
        if stream_log_path:
            with stream_lock:
                append_log(
                    stream_log_path,
                    "".join(
                        "{}{}".format(log_prefix, line)
                        for line in result.stdout.splitlines(keepends=True)
                    ),
                )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)


def _record_method_error(
    error: Exception,
    method_log: Path,
    root_log: Path,
    log_lock: threading.Lock,
    log_prefix: str,
) -> None:
    message = "ERROR: {} (see {})".format(error, method_log)
    append_log(method_log, message)
    with log_lock:
        append_log(root_log, "{}{}\n".format(log_prefix, message))
    print("{}{}".format(log_prefix, message), file=sys.stderr)


def recorded_exit_code(error: BaseException) -> int | None:
    if isinstance(error, subprocess.CalledProcessError) and isinstance(error.returncode, int):
        return error.returncode
    return None


def run_assembler(
    assembler: str,
    outdir: Path,
    filtered_r1: Path,
    filtered_r2: Path,
    thread_count: int,
    spades_kmers: list[int] | None,
    spades_careful: bool,
    root_log: Path,
    log_lock: threading.Lock,
    stages: StageRecord,
) -> tuple[str, bool]:
    method_log = outdir / "logs" / "{}.log".format(assembler)
    method_versions = outdir / "logs" / "{}.versions.txt".format(assembler)
    log_prefix = "[{}] ".format(assembler)
    append_log(method_log, "Assembler: {}\nThreads: {}".format(assembler, thread_count))
    with log_lock:
        append_log(
            root_log,
            "{}Starting method with {} threads".format(log_prefix, thread_count),
        )

    stages.start(assembler)
    try:
        arguments = [
            "--r1",
            str(filtered_r1),
            "--r2",
            str(filtered_r2),
            "--outdir",
            str(outdir / "assemblies" / assembler),
        ]
        script = "run_{}.py".format(assembler)
        if assembler == "spades":
            if spades_kmers:
                arguments += ["--kmers", ",".join(map(str, spades_kmers))]
            if spades_careful:
                arguments.append("--careful")
        run_script(
            script,
            arguments,
            method_log,
            method_versions,
            thread_count=thread_count,
            stream_log_path=root_log,
            stream_lock=log_lock,
            log_prefix=log_prefix,
        )
        contigs = outdir / "assemblies" / assembler / ASSEMBLY_CONTIGS[assembler]
        filtered_500 = (
            outdir / "assemblies" / assembler / "contigs_gt500bp.fasta"
        )
        filtered_1000 = (
            outdir / "assemblies" / assembler / "contigs_gt1000bp.fasta"
        )
        stages.note(assembler, "正在按长度过滤 contig")
        run_contig_filter(
            contigs,
            filtered_500,
            filtered_1000,
            method_log,
            root_log,
            log_lock,
            log_prefix,
        )
        stages.finish(assembler, exit_code=0)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        stages.finish(
            assembler, "failed", detail=str(error), exit_code=recorded_exit_code(error)
        )
        _record_method_error(error, method_log, root_log, log_lock, log_prefix)
        return assembler, False

    evaluations = [
        (contigs, outdir / "quast" / assembler, "original"),
        (
            filtered_500,
            outdir / "quast" / assembler / "gt500bp",
            ">500 bp",
        ),
        (
            filtered_1000,
            outdir / "quast" / assembler / "gt1000bp",
            ">1000 bp",
        ),
    ]
    for (evaluation, quast_dir, label), (evaluation_id, evaluation_label) in zip(
        evaluations, QUAST_EVALUATIONS
    ):
        stage_id = quast_stage_id(assembler, evaluation_id)
        try:
            if not evaluation.is_file() or evaluation.stat().st_size == 0:
                message = "Skipping QUAST for {} {}: no contigs meet threshold".format(
                    assembler, label
                )
                print("{}{}".format(log_prefix, message))
                append_log(method_log, message)
                with log_lock:
                    append_log(root_log, "{}{}\n".format(log_prefix, message))
                stages.start(stage_id)
                stages.finish(stage_id, "done", detail="没有达到阈值的 contig", exit_code=0)
                continue
            stages.start(stage_id, detail=evaluation_label)
            run_script(
                "run_quast.py",
                [
                    "--contigs",
                    str(evaluation),
                    "--outdir",
                    str(quast_dir),
                ],
                method_log,
                method_versions,
                thread_count=thread_count,
                stream_log_path=root_log,
                stream_lock=log_lock,
                log_prefix=log_prefix,
            )
            stages.finish(stage_id, exit_code=0)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            stages.finish(
                stage_id, "failed", detail=str(error), exit_code=recorded_exit_code(error)
            )
            _record_method_error(error, method_log, root_log, log_lock, log_prefix)
            return assembler, False
    with log_lock:
        append_log(root_log, "{}Method complete".format(log_prefix))
    return assembler, True


OUTCOME_SCHEMA_VERSION = 1


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
    destination = outdir / "assembly_outcome.json"
    temporary = outdir / ".assembly_outcome.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)


def merge_method_versions(
    assemblers: list[str], log_dir: Path, versions_path: Path
) -> None:
    with versions_path.open("a", encoding="utf-8") as destination:
        for assembler in assemblers:
            method_versions = log_dir / "{}.versions.txt".format(assembler)
            if method_versions.is_file():
                destination.write(method_versions.read_text(encoding="utf-8"))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if len(set(args.assembler)) != len(args.assembler):
        parser.error("each assembler may be selected only once")
    if (args.spades_kmers or args.spades_careful) and "spades" not in args.assembler:
        parser.error("--spades-kmers and --spades-careful require --assembler spades")
    cpu_count = logical_cpu_count()
    try:
        detected = read_cgroup(
            Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace"),
            Path("/sys/fs/cgroup"),
        )
    except OSError:
        detected = {"quota_cpus": None, "cpuset_cpus": None, "memory_bytes": None, "generation": None}
    cpu_limit, cpu_source = combine_cpu(cpu_count, detected["quota_cpus"], detected["cpuset_cpus"])
    if detected["generation"] and cpu_source == "cgroup":
        cpu_source = detected["generation"]
    method_threads = cpu_share(cpu_limit)
    try:
        memory_limit, memory_source = combine_memory(
            physical_memory_bytes(), detected["memory_bytes"]
        )
        if detected["generation"] and memory_source == "cgroup":
            memory_source = detected["generation"]
        method_memory_gib = memory_share_gib(memory_limit)
    except RuntimeError as error:
        parser.error(str(error))
    try:
        validate_reads(args.r1, args.r2)
        validate_sample(args.sample)
        outdir = Path(args.outdir)
        prepare_output_dir(outdir)
    except ValueError as error:
        parser.error(str(error))

    outdir = outdir.resolve()
    if args.job_id:
        try:
            job_token = str(uuid.UUID(args.job_id))
        except ValueError:
            parser.error("--job-id must be a UUID")
        (outdir / "process.json").write_text(
            json.dumps(
                {
                    "job_id": job_token,
                    "pid": os.getpid(),
                    "process_group_id": os.getpgid(0),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    log_path = outdir / "run.log"
    versions_path = outdir / "versions.txt"
    versions_path.write_text("", encoding="utf-8")
    append_log(
        log_path,
        "Sample: {}\nR1: {}\nR2: {}\nResults: {}\n{}".format(
            args.sample,
            args.r1,
            args.r2,
            outdir,
            allocation_log(
                cpu_count,
                cpu_limit,
                cpu_source,
                memory_limit,
                memory_source,
                method_threads,
                method_memory_gib,
            ),
        ),
    )

    filtered_r1 = outdir / "fastp" / "{}_R1.fastq.gz".format(args.sample)
    filtered_r2 = outdir / "fastp" / "{}_R2.fastq.gz".format(args.sample)
    selected = list(args.assembler)
    outcomes = {}
    stages = StageRecord(outdir / "stages.json", selected)
    try:
        stages.start("fastp")
        run_script(
            "run_fastp.py",
            [
                "--r1",
                args.r1,
                "--r2",
                args.r2,
                "--outdir",
                str(outdir / "fastp"),
                "--sample",
                args.sample,
            ],
            log_path,
            versions_path,
            thread_count=method_threads,
        )
        stages.finish("fastp", exit_code=0)

        log_dir = outdir / "logs"
        log_lock = threading.Lock()
        for assembler in selected:
            name, succeeded = run_assembler(
                assembler,
                outdir,
                filtered_r1,
                filtered_r2,
                method_threads,
                args.spades_kmers,
                args.spades_careful,
                log_path,
                log_lock,
                stages,
            )
            outcomes[name] = succeeded

        merge_method_versions(selected, log_dir, versions_path)
        successful = [assembler for assembler in selected if outcomes.get(assembler)]
        failed = [
            assembler for assembler in selected if assembler in outcomes and not outcomes[assembler]
        ]
        if successful:
            stages.start("comparison")
            run_summary(outdir, successful, log_path)
            stages.finish("comparison", exit_code=0)
        else:
            stages.finish(
                "comparison",
                "done",
                detail="没有成功的组装方法，未写比较汇总",
                exit_code=0,
            )
        if not successful:
            outcome, exit_code = "failed", 1
        elif failed:
            outcome, exit_code = "partial_success", 3
        else:
            outcome, exit_code = "succeeded", 0
        write_outcome(outdir, outcome, selected, successful, failed, exit_code)
        if failed:
            report_script_error(
                RuntimeError(
                    "assembler methods failed: {}".format(", ".join(failed))
                ),
                log_path,
            )
            return exit_code
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        stages.fail_running(str(error))
        try:
            write_outcome(
                outdir,
                "failed",
                selected,
                [assembler for assembler in selected if outcomes.get(assembler)],
                [
                    assembler
                    for assembler in selected
                    if assembler in outcomes and not outcomes[assembler]
                ],
                1,
            )
        except OSError:
            pass
        return report_script_error(error, log_path)

    print("Pipeline complete: {}".format(outdir))
    append_log(log_path, "Pipeline complete: {}".format(outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
