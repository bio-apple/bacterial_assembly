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

ASSEMBLER_SOFTWARE = {
    "spades": "SPAdes",
    "unicycler": "Unicycler",
    "shovill": "Shovill",
    "megahit": "MEGAHIT",
}
QUAST_EVALUATIONS = (
    ("original", "原始 contig"),
    ("gt500", ">500 bp"),
    ("gt1000", ">1000 bp"),
)
STAGE_STATES = ("waiting", "running", "done", "failed")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quast_stage_id(assembler: str, evaluation: str) -> str:
    return "{}:quast:{}".format(assembler, evaluation)


def _stage(
    stage_id: str,
    group: str,
    group_title: str,
    software: str,
    label: str,
    summary: str,
    kind: str,
) -> dict:
    return {
        "id": stage_id,
        "group": group,
        "group_title": group_title,
        "software": software,
        "label": label,
        "summary": summary,
        "kind": kind,
        "state": "waiting",
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "percent": None,
        "detail": None,
    }


def planned_stages(assemblers: list[str]) -> list[dict]:
    stages = [
        _stage("fastp", "fastp", "fastp", "fastp", "质量过滤", "质量过滤", "fastp")
    ]
    for name in assemblers:
        software = ASSEMBLER_SOFTWARE.get(name)
        if software is None:
            raise ValueError("unknown assembler: {}".format(name))
        stages.append(
            _stage(
                name,
                name,
                software,
                software,
                "组装",
                "{} · 组装".format(software),
                "assembly",
            )
        )
        for evaluation, evaluation_label in QUAST_EVALUATIONS:
            stages.append(
                _stage(
                    quast_stage_id(name, evaluation),
                    name,
                    software,
                    "QUAST",
                    evaluation_label,
                    "{} · QUAST {}".format(software, evaluation_label),
                    "quast",
                )
            )
    stages.append(
        _stage(
            "comparison",
            "comparison",
            "比较汇总",
            "比较汇总",
            "写出 comparison.tsv",
            "比较汇总",
            "comparison",
        )
    )
    return stages


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
    def __init__(self, path: Path, assemblers: list[str]):
        self.path = Path(path)
        self._lock = threading.Lock()
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
