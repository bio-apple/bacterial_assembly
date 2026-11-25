#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__:
    from .common import (
        add_output_arguments,
        add_read_arguments,
        assigned_threads,
        cpu_share,
        memory_share_gib,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
        validate_reads,
    )
else:
    from common import (
        add_output_arguments,
        add_read_arguments,
        assigned_threads,
        cpu_share,
        memory_share_gib,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
        validate_reads,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble paired-end reads with Shovill")
    add_read_arguments(parser)
    add_output_arguments(parser)
    args = parser.parse_args()
    threads = assigned_threads() if os.environ.get("_ASSEMBLY_ASSIGNED_THREADS") else cpu_share()
    memory_gib = memory_share_gib()
    outdir = Path(args.outdir)
    log_path, versions_path = reporting_paths(args, outdir)
    try:
        validate_reads(args.r1, args.r2)
        prepare_output_dir(outdir)
        record_version("Shovill", ["shovill", "--version"], versions_path, log_path)
        record_version("SKESA", ["skesa", "--version"], versions_path, log_path)
        command = [
            "shovill",
            "--R1",
            args.r1,
            "--R2",
            args.r2,
            "--outdir",
            str(outdir),
            "--cpus",
            str(threads),
            "--ram",
            str(memory_gib),
            "--assembler",
            "skesa",
        ]
        run_command(command, "Shovill (SKESA) assembly", log_path)
        if not (outdir / "contigs.fa").is_file():
            raise RuntimeError("Shovill did not create contigs.fa")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        return report_script_error(error, log_path)


if __name__ == "__main__":
    sys.exit(main())
