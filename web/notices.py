"""Owner-scoped lab notices.

A disk notice is recorded only from a real event: shutil.disk_usage on
ASSEMBLY_DATA_ROOT / DATA_ROOT reports less than LOW_DISK_FREE_BYTES (5 GiB)
free, or a write raises OSError with errno ENOSPC. Nothing invents one on a timer.
"""

import errno
import shutil
import sqlite3
from pathlib import Path

LOW_DISK_FREE_BYTES = 5 * 1024 ** 3

JOB_VERBS = {
    "submitted": "已提交",
    "running": "开始运行",
    "failed": "执行失败",
    "succeeded": "成功完成",
    "partial_success": "部分完成",
    "cancelled": "已取消",
    "cancelling": "正在取消",
}


def add_notice(db: sqlite3.Connection, owner: str, kind: str, message: str, job_id: str | None = None) -> None:
    if not owner or not message:
        return
    db.execute(
        "INSERT INTO notices (owner, job_id, kind, message) VALUES (?, ?, ?, ?)",
        (owner, job_id, kind, message),
    )


def note_job(db: sqlite3.Connection, owner: str, sample: str, job_id: str, kind: str) -> None:
    verb = JOB_VERBS.get(kind)
    if verb is None:
        return
    add_notice(db, owner, kind, "{} {}".format(sample or "样本", verb), job_id)


def note_disk(db: sqlite3.Connection, owner: str, sample: str | None = None, job_id: str | None = None) -> None:
    message = "{} 存储空间不足".format(sample) if sample else "存储空间不足"
    add_notice(db, owner, "disk", message, job_id)


def volume_is_low(data_root: Path) -> bool:
    """True when the data volume is under 5 GiB free, or disk_usage itself raises ENOSPC."""
    try:
        usage = shutil.disk_usage(data_root)
    except OSError as error:
        if error.errno == errno.ENOSPC:
            return True
        raise
    return usage.free < LOW_DISK_FREE_BYTES
