"""Run ValidateFastq after an upload is assembled.

ValidateFastq 0.1.1 logs a record error and still exits 0. A file is accepted
only when the process exits 0 and the log says no errors were found.
"""

import gzip
import json
import os
import re
import subprocess
from pathlib import Path

from web.errors import FASTQ_GZIP, FASTQ_RECORDS

ERROR_MARK = "Error found at readnumber"
SUCCESS_MARK = "no errors found"
READ_NUMBER = re.compile(r"Error found at readnumber:\s*(\d+)")


class FastqRecordError(ValueError):
    pass


class FastqCheckUnavailable(RuntimeError):
    pass


def records_accepted(returncode: int, output: str) -> bool:
    if returncode != 0 or ERROR_MARK in output:
        return False
    return SUCCESS_MARK in output


def record_failure_message(output: str) -> str:
    match = READ_NUMBER.search(output or "")
    if match is None:
        return FASTQ_RECORDS
    return "FASTQ 格式错误。第 {} 条记录无法通过检查，文件没有进入可分析状态。".format(
        int(match.group(1))
    )


def check_command(configured) -> list[str]:
    if configured is None:
        return ["biopet-validatefastq"]
    if isinstance(configured, str):
        return [configured]
    return [str(part) for part in configured]


def _gzip_name(path: Path) -> bool:
    suffixes = "".join(path.suffixes).lower()
    return suffixes.endswith(".gz")


def gzip_is_intact(path: Path) -> bool:
    if not _gzip_name(path):
        return True
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1024 * 1024):
                pass
    except (OSError, EOFError, gzip.BadGzipFile):
        return False
    return True


def write_validation(directory: Path, name: str, payload: dict, log_text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "{}.log".format(name)).write_text(log_text, encoding="utf-8")
    (directory / "{}.json".format(name)).write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def check_fastq_records(path: Path, command=None, log_dir: Path | None = None) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 1:
        raise FastqRecordError(FASTQ_RECORDS)
    if not gzip_is_intact(path):
        if log_dir is not None:
            write_validation(
                log_dir,
                "fastq_validation",
                {"ok": False, "gzip": "failed", "path": path.name},
                FASTQ_GZIP + "\n",
            )
        raise FastqRecordError(FASTQ_GZIP)
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
    accepted = records_accepted(result.returncode, output)
    if log_dir is not None:
        write_validation(
            log_dir,
            "fastq_validation",
            {
                "ok": accepted,
                "gzip": "ok" if _gzip_name(path) else "not_gzip",
                "tool": "biopet-validatefastq",
                "returncode": result.returncode,
                "path": path.name,
            },
            output[-8000:],
        )
    if not accepted:
        raise FastqRecordError(record_failure_message(output))


def pair_key(header: str) -> str:
    token = header[1:].strip().split()[0]
    if len(token) >= 2 and token[-2] in "/." and token[-1] in "12":
        return token[:-2]
    return token


def _open_text(path: Path):
    if _gzip_name(path):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def iter_record_ids(path: Path):
    try:
        handle = _open_text(path)
    except OSError as error:
        raise FastqRecordError(FASTQ_GZIP if _gzip_name(path) else FASTQ_RECORDS) from error
    with handle:
        while True:
            header = handle.readline()
            if header == "":
                return
            sequence = handle.readline()
            separator = handle.readline()
            quality = handle.readline()
            if (
                not header.startswith("@")
                or sequence == ""
                or not separator.startswith("+")
                or quality == ""
                or len(sequence.strip()) != len(quality.strip())
            ):
                raise FastqRecordError(FASTQ_RECORDS)
            yield pair_key(header)


def check_read_pair(r1: Path, r2: Path, log_dir: Path) -> None:
    left = right = 0
    mismatch_at = None
    try:
        stream_2 = iter_record_ids(r2)
        for left, key_1 in enumerate(iter_record_ids(r1), start=1):
            try:
                key_2 = next(stream_2)
            except StopIteration:
                right = left - 1
                break
            right = left
            if key_1 != key_2 and mismatch_at is None:
                mismatch_at = left
        else:
            extra = next(stream_2, None)
            if extra is not None:
                right = left + 1
                while True:
                    try:
                        next(stream_2)
                        right += 1
                    except StopIteration:
                        break
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        write_validation(log_dir, "fastq_pair", {"ok": False, "gzip": "failed"}, FASTQ_GZIP + "\n")
        raise FastqRecordError(FASTQ_GZIP) from error
    if left != right:
        message = "FASTQ 格式错误。R1 有 {} 条记录，R2 有 {} 条记录，数量不一致，文件没有进入分析。".format(
            left, right
        )
        write_validation(
            log_dir,
            "fastq_pair",
            {"ok": False, "r1_records": left, "r2_records": right},
            message + "\n",
        )
        raise FastqRecordError(message)
    if mismatch_at is not None:
        message = "FASTQ 格式错误。第 {} 条 read 编号不一致，文件没有进入分析。".format(mismatch_at)
        write_validation(
            log_dir,
            "fastq_pair",
            {"ok": False, "r1_records": left, "r2_records": right, "mismatch_at": mismatch_at},
            message + "\n",
        )
        raise FastqRecordError(message)
    message = "R1 和 R2 各有 {} 条记录，read 编号一致。".format(left)
    write_validation(
        log_dir,
        "fastq_pair",
        {"ok": True, "r1_records": left, "r2_records": right},
        message + "\n",
    )
