"""Run ValidateFastq after an upload is assembled.

ValidateFastq 0.1.1 logs a record error and still exits 0. A file is accepted
only when the process exits 0 and the log says no errors were found.
"""

import os
import subprocess
from pathlib import Path

from web.errors import FASTQ_RECORDS

ERROR_MARK = "Error found at readnumber"
SUCCESS_MARK = "no errors found"


class FastqRecordError(ValueError):
    pass


class FastqCheckUnavailable(RuntimeError):
    pass


def records_accepted(returncode: int, output: str) -> bool:
    if returncode != 0 or ERROR_MARK in output:
        return False
    return SUCCESS_MARK in output


def check_command(configured) -> list[str]:
    if configured is None:
        return ["biopet-validatefastq"]
    if isinstance(configured, str):
        return [configured]
    return [str(part) for part in configured]


def check_fastq_records(path: Path, command=None) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 1:
        raise FastqRecordError(FASTQ_RECORDS)
    try:
        result = subprocess.run(
            [*check_command(command), "-i", os.fspath(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise FastqCheckUnavailable("ValidateFastq could not be started") from error
    output = "{}\n{}".format(result.stdout or "", result.stderr or "")
    if not records_accepted(result.returncode, output):
        raise FastqRecordError(FASTQ_RECORDS)
