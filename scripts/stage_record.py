"""Stage record for one bacterial assembly job.

Task status (queued / running / cancelling / cancelled / succeeded / failed) stays on the job row.
This file only records pipeline stages. Percents stay empty unless a caller
supplies a real fraction; assembly tools do not.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from scripts.pipeline_plan import (
    ASSEMBLER_SOFTWARE,
    QUAST_EVALUATIONS,
    planned_stages,
    quast_stage_id,
)

STAGE_STATES = ("waiting", "running", "done", "failed")

# Re-export plan symbols for Worker callers that historically imported stage_record.
__all__ = (
    "ASSEMBLER_SOFTWARE",
    "QUAST_EVALUATIONS",
    "STAGE_STATES",
    "StageRecord",
    "build_progress",
    "group_stages",
    "load_stage_record",
    "normalize_percent",
    "planned_stages",
    "quast_stage_id",
    "utc_now",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group_state(states: list[str]) -> str:
    if "running" in states:
        return "running"
    if "failed" in states:
        return "failed"
    if states and all(state == "done" for state in states):
        return "done"
    if any(state == "done" for state in states):
        return "running"
    return "waiting"


def group_stages(stages: list[dict]) -> list[dict]:
    groups = []
    index = {}
    for stage in stages:
        key = stage["group"]
        if key not in index:
            group = {"id": key, "title": stage["group_title"], "steps": []}
            index[key] = group
            groups.append(group)
        index[key]["steps"].append(stage)
    for group in groups:
        group["state"] = _group_state([step["state"] for step in group["steps"]])
    return groups


def normalize_percent(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number > 100:
        return None
    if number.is_integer():
        return int(number)
    return round(number, 1)


def load_stage_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"updated_at": None, "stages": []}
    if not isinstance(data, dict):
        return {"updated_at": None, "stages": []}
    stages = data.get("stages")
    if not isinstance(stages, list):
        stages = []
    kept = [
        stage
        for stage in stages
        if isinstance(stage, dict) and isinstance(stage.get("id"), str)
    ]
    updated = data.get("updated_at")
    return {
        "updated_at": updated if isinstance(updated, str) else None,
        "stages": kept,
    }


def _select_current(stages: list[dict], job_status: str) -> dict | None:
    for stage in stages:
        if stage["state"] == "running":
            return stage
    if job_status == "queued":
        return None
    for stage in reversed(stages):
        if stage["state"] in ("done", "failed"):
            return stage
    return None


def build_progress(assemblers: list[str], record: dict, job_status: str) -> dict:
    recorded = {
        stage["id"]: stage
        for stage in record.get("stages") or []
        if isinstance(stage, dict) and isinstance(stage.get("id"), str)
    }
    stages = []
    for stage in planned_stages(assemblers):
        merged = dict(stage)
        overlay = recorded.get(stage["id"])
        if overlay:
            state = overlay.get("state")
            merged["state"] = state if state in STAGE_STATES else "waiting"
            for key in ("started_at", "finished_at", "updated_at", "detail"):
                if key in overlay:
                    merged[key] = overlay[key]
            merged["percent"] = normalize_percent(overlay.get("percent"))
        stages.append(merged)
    current = _select_current(stages, job_status)
    current_view = {
        "id": None,
        "software": None,
        "label": None,
        "state": None,
        "percent": None,
        "detail": None,
    }
    if current is not None:
        current_view = {
            "id": current["id"],
            "software": current["software"],
            "label": current["summary"],
            "state": current["state"],
            "percent": current["percent"],
            "detail": current.get("detail"),
        }
    return {
        "status": job_status,
        "updated_at": record.get("updated_at"),
        "current": current_view,
        "stages": stages,
        "groups": group_stages(stages),
    }


class StageRecord:
    def __init__(self, path: Path, assemblers: list[str], *, resume: bool = False):
        self.path = Path(path)
        self._lock = threading.Lock()
        if resume and self.path.is_file():
            loaded = load_stage_record(self.path)
            if loaded.get("stages"):
                self.data = {
                    "updated_at": loaded.get("updated_at") or utc_now(),
                    "stages": loaded["stages"],
                }
                return
        self.data = {
            "updated_at": utc_now(),
            "stages": planned_stages(list(assemblers)),
        }
        self._write()

    def _find(self, stage_id: str) -> dict:
        for stage in self.data["stages"]:
            if stage["id"] == stage_id:
                return stage
        raise KeyError(stage_id)

    def _write(self) -> None:
        self.data["updated_at"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def start(self, stage_id: str, detail: str | None = None) -> None:
        with self._lock:
            stage = self._find(stage_id)
            now = utc_now()
            stage["state"] = "running"
            stage["started_at"] = stage["started_at"] or now
            stage["finished_at"] = None
            stage["updated_at"] = now
            if detail is not None:
                stage["detail"] = detail[:500]
            self._write()

    def note(self, stage_id: str, detail: str) -> None:
        with self._lock:
            stage = self._find(stage_id)
            stage["detail"] = detail[:500]
            stage["updated_at"] = utc_now()
            self._write()

    def finish(
        self,
        stage_id: str,
        state: str = "done",
        detail: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        if state not in ("done", "failed"):
            raise ValueError("stage finish state must be done or failed")
        if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
            raise ValueError("exit_code must be an integer")
        with self._lock:
            stage = self._find(stage_id)
            now = utc_now()
            stage["state"] = state
            stage["started_at"] = stage["started_at"] or now
            stage["finished_at"] = now
            stage["updated_at"] = now
            if detail is not None:
                stage["detail"] = detail[:500]
            if exit_code is not None:
                stage["exit_code"] = exit_code
            self._write()

    def fail_running(self, detail: str | None = None) -> None:
        with self._lock:
            now = utc_now()
            changed = False
            for stage in self.data["stages"]:
                if stage["state"] != "running":
                    continue
                stage["state"] = "failed"
                stage["finished_at"] = now
                stage["updated_at"] = now
                if detail:
                    stage["detail"] = detail[:500]
                changed = True
            if changed:
                self._write()
