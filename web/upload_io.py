"""Upload byte assembly and session paths for the 分析工作台."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from pathlib import Path

from werkzeug.utils import secure_filename

from web.storage_paths import is_inside

FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")


def session_directory(staging_root: Path, upload_id: str) -> Path:
    try:
        parsed = uuid.UUID(str(upload_id))
    except (TypeError, ValueError, AttributeError):
        raise LookupError("Upload not found")
    root = staging_root.resolve()
    directory = (root / str(parsed)).resolve()
    if directory == root or not is_inside(root, directory):
        raise LookupError("Upload not found")
    return directory


def valid_fastq_filename(filename: str) -> bool:
    safe_name = secure_filename(filename or "").lower()
    return bool(safe_name) and safe_name.endswith(FASTQ_SUFFIXES)


def fastq_extension(filename: str) -> str:
    lowered = secure_filename(filename).lower()
    return next(suffix for suffix in FASTQ_SUFFIXES if lowered.endswith(suffix))


def validated_fastq_name(filename: object) -> str | None:
    if not isinstance(filename, str) or not filename or len(filename) > 240:
        return None
    if any(mark in filename for mark in ("..", "/", "\\", "\x00")):
        return None
    safe = secure_filename(filename)
    if safe.lower() != filename.lower() or not valid_fastq_filename(safe):
        return None
    return safe


def parse_byte_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        size = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value):
        size = int(value)
    else:
        return None
    if size < 1 or size > 2**62:
        return None
    return size


def chunk_byte_length(total: int, chunk_size: int, index: int, chunk_count: int) -> int:
    if index < 0 or index >= chunk_count:
        raise ValueError("分片序号超出范围。")
    if index == chunk_count - 1:
        return total - chunk_size * (chunk_count - 1)
    return chunk_size


def write_exact_stream(stream, destination: Path, expected: int) -> None:
    partial = destination.with_name(destination.name + ".part")
    written = 0
    try:
        with partial.open("wb") as handle:
            while written < expected:
                block = stream.read(min(1024 * 1024, expected - written))
                if not block:
                    break
                handle.write(block)
                written += len(block)
            extra = stream.read(1)
        if extra or written != expected:
            raise ValueError("分片大小与上传会话不一致。")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def assemble_upload(
    chunk_dir: Path, total: int, chunk_size: int, chunk_count: int, destination: Path
) -> str:
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        with partial.open("wb") as output:
            for index in range(chunk_count):
                expected = chunk_byte_length(total, chunk_size, index, chunk_count)
                source = chunk_dir / "{:08d}".format(index)
                if (
                    not source.is_file()
                    or source.stat().st_size != expected
                    or not is_inside(chunk_dir, source)
                ):
                    raise ValueError("仍有分片未上传。")
                with source.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        output.write(block)
        if partial.stat().st_size != total:
            raise ValueError("仍有分片未上传。")
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def received_indexes(staging_root: Path, row: sqlite3.Row) -> list[int]:
    chunk_dir = session_directory(staging_root, row["id"]) / "chunks"
    found = []
    if not chunk_dir.is_dir():
        return found
    for index in range(row["chunk_count"]):
        expected = chunk_byte_length(row["size"], row["chunk_size"], index, row["chunk_count"])
        path = chunk_dir / "{:08d}".format(index)
        try:
            if path.is_file() and path.stat().st_size == expected and is_inside(chunk_dir, path):
                found.append(index)
        except OSError:
            continue
    return found


def serialize_upload(row: sqlite3.Row, received: list[int]) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "filename": row["filename"],
        "size": row["size"],
        "chunk_size": row["chunk_size"],
        "chunk_count": row["chunk_count"],
        "received": received,
        "status": row["status"],
        "sha256": row["sha256"],
    }
