"""Report file write/send adapter for the 分析工作台."""

from __future__ import annotations

import errno
import os
import uuid
from pathlib import Path

from flask import abort, current_app, send_file

from web.storage_paths import is_inside


def write_regular_file(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise OSError(errno.ELOOP, "report path is a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def store_report_files(job_id: str, html: str, tsv: str) -> tuple[Path, Path]:
    root = Path(current_app.config["REPORTS_ROOT"]).resolve()
    directory = root / str(uuid.UUID(str(job_id)))
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise OSError(errno.ELOOP, "report directory is not a real directory")
    if directory != root and root not in directory.resolve(strict=False).parents:
        raise OSError(errno.EPERM, "report directory escapes the reports root")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or not is_inside(root, directory):
        raise OSError(errno.ELOOP, "report directory is not a real directory")
    html_path = directory / "report.html"
    tsv_path = directory / "report.tsv"
    write_regular_file(html_path, html.encode("utf-8"))
    write_regular_file(tsv_path, tsv.encode("utf-8"))
    return html_path, tsv_path


def send_regular_report(path: Path, download_name: str, mimetype: str):
    root = Path(current_app.config["REPORTS_ROOT"]).resolve()
    if path.is_symlink() or not path.is_file() or not is_inside(root, path):
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype,
    )
