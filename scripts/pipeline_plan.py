"""Planned pipeline steps for one bacterial assembly analysis task.

This is the plan skeleton shared by Web 步骤读模型 and Worker StageRecord writes.
File-state vocabulary (waiting/done) stays in stage_record; step statuses live in web.steps.
"""

from __future__ import annotations

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
