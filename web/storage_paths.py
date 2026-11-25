"""数据根下的路径约束与任务日志追加（供 Web 与 worker 共用）。"""

from __future__ import annotations

from pathlib import Path


def is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def contained_path(root: Path, raw: str | Path) -> Path:
    root_resolved = root.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("path is outside the data root")
    return resolved


def append_job_log(log_path: str | Path, text: str) -> None:
    path = Path(log_path)
    line = text if text.endswith("\n") else text + "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return
