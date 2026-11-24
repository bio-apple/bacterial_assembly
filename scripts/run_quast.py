#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path

if __package__:
    from .common import (
        add_output_arguments,
        assigned_threads,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
    )
else:
    from common import (
        add_output_arguments,
        assigned_threads,
        prepare_output_dir,
        record_version,
        report_script_error,
        reporting_paths,
        run_command,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate an assembly with QUAST")
    parser.add_argument("--contigs", required=True, help="Assembly FASTA file")
    add_output_arguments(parser)
    args = parser.parse_args()
    threads = assigned_threads()
    outdir = Path(args.outdir)
    log_path, versions_path = reporting_paths(args, outdir)
    try:
        contigs = Path(args.contigs)
        if not contigs.is_file() or not contigs.stat().st_size:
            raise ValueError("contigs file is missing or empty: {}".format(contigs))
        prepare_output_dir(outdir)
        record_version("QUAST", ["quast.py", "--version"], versions_path, log_path)
        run_command(
            [
                "quast.py",
                "--threads",
                str(threads),
                "-o",
                str(outdir),
                str(contigs),
            ],
            "QUAST reference-free evaluation",
            log_path,
        )
        if not (outdir / "report.tsv").is_file():
            raise RuntimeError("QUAST did not create report.tsv")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        return report_script_error(error, log_path)


if __name__ == "__main__":
    sys.exit(main())
