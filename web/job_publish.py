"""Publish work → output and archive finished analysis jobs."""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path


def publish_work_to_output(source: Path, destination: Path) -> None:
    """Copy work tree into jobs/<id>/output, then clear work contents."""
    incoming = destination.with_name(destination.name + ".incoming")
    if incoming.exists():
        shutil.rmtree(incoming)
    shutil.copytree(source, incoming)
    if destination.exists():
        shutil.rmtree(destination)
    incoming.rename(destination)
    for child in source.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def archive_results_dir(
    job_id: str,
    archived_dir: Path,
    data_root: Path | None = None,
    *,
    archive_dir: Path | None = None,
) -> Path | None:
    if archive_dir is None:
        root = data_root
        if root is None:
            raw = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
            if not raw:
                return None
            root = Path(raw)
        archive_dir = root / "archive"
    archive_path = Path(archive_dir) / "{}.tar.gz".format(job_id)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial = archive_path.with_name(archive_path.name + ".partial")
    try:
        partial.unlink(missing_ok=True)
        with tarfile.open(partial, "w:gz") as archive:
            archive.add(
                archived_dir,
                arcname="results",
                recursive=True,
                filter=lambda member: None if member.issym() else member,
            )
        if partial.is_file() and partial.stat().st_size > 0:
            partial.replace(archive_path)
            return archive_path
    except OSError:
        partial.unlink(missing_ok=True)
    return None
