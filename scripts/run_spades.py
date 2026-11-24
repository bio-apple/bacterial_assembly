#!/usr/bin/env python3
import argparse
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    from .common import (
        add_output_arguments,
        add_read_arguments,
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
        cpu_share,
        memory_share_gib,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
        validate_reads,
    )


def parse_kmers(value: str) -> list[int]:
    if not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", value):
        raise argparse.ArgumentTypeError("use comma-separated positive integers")
    kmers = [int(item) for item in value.split(",")]
    if any(kmer > 127 or kmer % 2 == 0 for kmer in kmers):
        raise argparse.ArgumentTypeError("k-mer sizes must be odd and at most 127")
    return kmers


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble paired-end reads with SPAdes")
    add_read_arguments(parser)
    add_output_arguments(parser)
    parser.add_argument("--kmers", type=parse_kmers)
    parser.add_argument("--careful", action="store_true")
    args = parser.parse_args()
    threads = cpu_share()
    memory_gib = memory_share_gib()
    outdir = Path(args.outdir)
    log_path, versions_path = reporting_paths(args, outdir)
    try:
        validate_reads(args.r1, args.r2)
        prepare_output_dir(outdir)
        record_version("SPAdes", ["spades.py", "--version"], versions_path, log_path)
        command = [
            "spades.py",
            "-1",
            args.r1,
            "-2",
            args.r2,
            "-o",
            str(outdir),
            "-t",
            str(threads),
            "-m",
            str(memory_gib),
        ]
        if args.kmers:
            command.extend(["-k", ",".join(str(kmer) for kmer in args.kmers)])
        if args.careful:
            command.append("--careful")
        run_command(command, "SPAdes assembly", log_path)
        if not (outdir / "contigs.fasta").is_file():
            raise RuntimeError("SPAdes did not create contigs.fasta")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        return report_script_error(error, log_path)


if __name__ == "__main__":
    sys.exit(main())
