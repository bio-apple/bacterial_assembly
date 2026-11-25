import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def logical_cpu_count() -> int:
    return os.cpu_count() or 1


def _load_limit(name: str):
    try:
        from scripts import resource_limits
    except ImportError:
        import resource_limits
    return getattr(resource_limits, name)


def cpu_share(cpu_count: int | None = None) -> int:
    if cpu_count is None:
        available = _load_limit("limited_cpu_count")(logical_cpu_count())
    else:
        available = cpu_count
    if available < 1:
        raise ValueError("cpu_count must be positive")
    return max(1, (available * 9) // 10)


def physical_memory_bytes() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError) as error:
        raise RuntimeError("could not read physical system memory") from error
    if pages < 1 or page_size < 1:
        raise RuntimeError("could not read physical system memory")
    return pages * page_size


def memory_share_gib(total_bytes: int | None = None) -> int:
    if total_bytes is None:
        total = _load_limit("limited_memory_bytes")(physical_memory_bytes())
    else:
        total = total_bytes
    if total < 1:
        raise RuntimeError("system memory is unavailable")
    return max(1, (total * 9) // 10 // (1024 ** 3))


def assigned_threads() -> int:
    assigned = os.environ.get("_ASSEMBLY_ASSIGNED_THREADS")
    if assigned is None:
        return logical_cpu_count()
    try:
        threads = int(assigned)
    except ValueError as error:
        raise RuntimeError("invalid internal assembly thread allocation") from error
    if threads < 1:
        raise RuntimeError("internal assembly thread allocation must be positive")
    return threads


def add_read_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--r1", required=True, help="R1 FASTQ/FASTQ.GZ file")
    parser.add_argument("--r2", required=True, help="R2 FASTQ/FASTQ.GZ file")


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--outdir", required=True, help="Output directory")
    parser.add_argument("--log", help="Log file (default: sibling run.log)")
    parser.add_argument(
        "--versions-file", help="Version record (default: sibling versions.txt)"
    )


def validate_reads(r1: str, r2: str) -> None:
    path1 = Path(r1)
    path2 = Path(r2)
    for label, path in (("R1", path1), ("R2", path2)):
        if not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
            raise ValueError("{} is missing, unreadable or empty: {}".format(label, path))
    if path1.resolve() == path2.resolve():
        raise ValueError("R1 and R2 must be different files")


def prepare_output_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
        raise ValueError("output directory must be empty: {}".format(path))
    path.mkdir(parents=True, exist_ok=True)


def reporting_paths(args: argparse.Namespace, outdir: Path) -> tuple[Path, Path]:
    parent = outdir.parent
    log_path = Path(args.log) if args.log else parent / "run.log"
    versions_path = (
        Path(args.versions_file) if args.versions_file else parent / "versions.txt"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    versions_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path, versions_path


def append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(text)
        if not text.endswith("\n"):
            log_file.write("\n")


def _uses_docker_runtime(command: Sequence[str]) -> bool:
    try:
        from scripts.docker_executor import tool_runtime
        from scripts.software_registry import resolve_tool
    except ImportError:
        from docker_executor import tool_runtime
        from software_registry import resolve_tool

    return tool_runtime() == "docker" and resolve_tool(command) is not None


def record_version(
    name: str,
    command: Sequence[str],
    versions_path: Path,
    log_path: Path,
) -> None:
    if _uses_docker_runtime(command):
        try:
            from scripts.docker_executor import get_executor
        except ImportError:
            from docker_executor import get_executor

        scratch = log_path.parent / ".version-{}.log".format(Path(log_path).stem)
        get_executor().run_tool(command, "{} version".format(name), scratch)
        output = scratch.read_text(encoding="utf-8") if scratch.is_file() else ""
        scratch.unlink(missing_ok=True)
        # Prefer the last non-header line from the tool itself.
        lines = [
            line.strip()
            for line in output.splitlines()
            if line.strip()
            and not line.startswith("== ")
            and not line.startswith("DOCKER IMAGE:")
            and not line.startswith("COMMAND:")
        ]
        version = " ".join((lines[-1] if lines else output).split())
        if not version:
            raise RuntimeError("could not get {} version from docker tool".format(name))
        line = "{}\t{}\n".format(name, version)
        with versions_path.open("a", encoding="utf-8") as versions_file:
            versions_file.write(line)
        append_log(log_path, "VERSION: {}".format(line.rstrip()))
        return

    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.returncode:
        raise RuntimeError(
            "could not get {} version (exit code {})".format(name, result.returncode)
        )
    version = " ".join(result.stdout.split())
    line = "{}\t{}\n".format(name, version)
    with versions_path.open("a", encoding="utf-8") as versions_file:
        versions_file.write(line)
    append_log(log_path, "VERSION: {}".format(line.rstrip()))


def run_command(command: Sequence[str], label: str, log_path: Path) -> None:
    if _uses_docker_runtime(command):
        try:
            from scripts.docker_executor import get_executor
        except ImportError:
            from docker_executor import get_executor

        get_executor().run_tool(command, label, log_path)
        return

    command_line = shlex.join(command)
    header = "\n== {} ==\nCOMMAND: {}\n".format(label, command_line)
    print(header, end="", flush=True)
    append_log(log_path, header)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise RuntimeError("could not start {}: {}".format(label, error)) from error

    assert process.stdout is not None
    with log_path.open("a", encoding="utf-8") as log_file:
        for line in process.stdout:
            print(line, end="", flush=True)
            log_file.write(line)
            log_file.flush()
    return_code = process.wait()
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def report_script_error(error: Exception, log_path: Path) -> int:
    message = "ERROR: {} (see {})".format(error, log_path)
    print(message, file=sys.stderr)
    append_log(log_path, message)
    return 1


def validate_sample(sample: str) -> None:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", sample):
        raise ValueError(
            "sample name must start with a letter or digit and contain only "
            "letters, digits, dot, underscore or hyphen"
        )
