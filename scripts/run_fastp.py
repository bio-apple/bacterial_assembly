#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path

if __package__:
    from .common import (
        add_output_arguments,
        add_read_arguments,
        assigned_threads,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
        validate_reads,
        validate_sample,
    )
else:
    from common import (
        add_output_arguments,
        add_read_arguments,
        assigned_threads,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
        validate_reads,
        validate_sample,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter paired-end reads with fastp")
    add_read_arguments(parser)
    add_output_arguments(parser)
    parser.add_argument("--sample", default="sample")
    args = parser.parse_args()
    threads = assigned_threads()
    outdir = Path(args.outdir)
    log_path, versions_path = reporting_paths(args, outdir)
    try:
        validate_reads(args.r1, args.r2)
        validate_sample(args.sample)
        prepare_output_dir(outdir)
        record_version("fastp", ["fastp", "--version"], versions_path, log_path)
        command = [
            "fastp",
            "--thread",
            str(threads),
            "--in1",
            args.r1,
            "--in2",
            args.r2,
            "--out1",
            str(outdir / "{}_R1.fastq.gz".format(args.sample)),
            "--out2",
            str(outdir / "{}_R2.fastq.gz".format(args.sample)),
            "--json",
            str(outdir / "{}_fastp.json".format(args.sample)),
            "--html",
            str(outdir / "{}_fastp.html".format(args.sample)),
        ]
        run_command(command, "fastp filtering and reports", log_path)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        return report_script_error(error, log_path)


if __name__ == "__main__":
    sys.exit(main())
