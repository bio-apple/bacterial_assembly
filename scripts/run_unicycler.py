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
        append_log,
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
        append_log,
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
    parser = argparse.ArgumentParser(
        description="Assemble Illumina paired-end reads with Unicycler"
    )
    add_read_arguments(parser)
    add_output_arguments(parser)
    args = parser.parse_args()
    threads = assigned_threads() if os.environ.get("_ASSEMBLY_ASSIGNED_THREADS") else cpu_share()
    memory_gib = memory_share_gib()
    outdir = Path(args.outdir)
    log_path, versions_path = reporting_paths(args, outdir)
    try:
        from scripts.docker_executor import tool_runtime
    except ImportError:
        from docker_executor import tool_runtime

    if tool_runtime() == "docker":
        unicycler = ["unicycler"]
    else:
        unicycler = ["micromamba", "run", "-n", "unicycler", "unicycler"]
    try:
        validate_reads(args.r1, args.r2)
        prepare_output_dir(outdir)
        record_version(
            "Unicycler", unicycler + ["--version"], versions_path, log_path
        )
        if tool_runtime() == "docker":
            # Bundled in the unicycler image; do not resolve spades.py to the SPAdes tool image.
            line = "Unicycler SPAdes\tbundled in bacterial-assembly-unicycler:0.5.1\n"
            with versions_path.open("a", encoding="utf-8") as versions_file:
                versions_file.write(line)
            append_log(log_path, "VERSION: {}".format(line.rstrip()))
        else:
            record_version(
                "Unicycler SPAdes",
                [
                    "micromamba",
                    "run",
                    "-n",
                    "unicycler",
                    "spades.py",
                    "--version",
                ],
                versions_path,
                log_path,
            )
        command = unicycler + [
            "-1",
            args.r1,
            "-2",
            args.r2,
            "-o",
            str(outdir),
            "-t",
            str(threads),
            "--spades_options",
            "-m {}".format(memory_gib),
        ]
        run_command(command, "Unicycler Illumina-only assembly", log_path)
        if not (outdir / "assembly.fasta").is_file():
            raise RuntimeError("Unicycler did not create assembly.fasta")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        return report_script_error(error, log_path)


if __name__ == "__main__":
    sys.exit(main())
