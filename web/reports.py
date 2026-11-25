"""Assemble job detail and export figures from recorded files only."""

import csv
import io
import json
import re

ASSEMBLER_LABELS = {
    "spades": "SPAdes",
    "unicycler": "Unicycler",
    "shovill": "Shovill",
    "megahit": "MEGAHIT",
}
# Column names written by scripts/summarize_quast.py.
KNOWN_METRICS = (
    "# contigs",
    "Total length",
    "Largest contig",
    "N50",
    "L50",
    "GC (%)",
)
CHART_METRICS = (
    ("N50", "N50"),
    ("# contigs", "Contig 数量"),
    ("Total length", "基因组长度"),
)
QC_PAIRS = (
    ("total_reads", "reads", "count"),
    ("total_bases", "碱基数", "count"),
    ("q20_rate", "Q20", "rate"),
    ("q30_rate", "Q30", "rate"),
    ("gc_content", "GC", "rate"),
)
FASTP_FIELDS = (
    ("summary.before_filtering.total_reads", "过滤前总 reads"),
    ("summary.before_filtering.total_bases", "过滤前总碱基数"),
    ("summary.before_filtering.q20_rate", "过滤前 Q20 比例"),
    ("summary.before_filtering.q30_rate", "过滤前 Q30 比例"),
    ("summary.before_filtering.gc_content", "过滤前 GC 比例"),
    ("summary.before_filtering.read1_mean_length", "过滤前 R1 平均长度"),
    ("summary.before_filtering.read2_mean_length", "过滤前 R2 平均长度"),
    ("summary.after_filtering.total_reads", "过滤后总 reads"),
    ("summary.after_filtering.total_bases", "过滤后总碱基数"),
    ("summary.after_filtering.q20_rate", "过滤后 Q20 比例"),
    ("summary.after_filtering.q30_rate", "过滤后 Q30 比例"),
    ("summary.after_filtering.gc_content", "过滤后 GC 比例"),
    ("summary.after_filtering.read1_mean_length", "过滤后 R1 平均长度"),
    ("summary.after_filtering.read2_mean_length", "过滤后 R2 平均长度"),
    ("filtering_result.passed_filter_reads", "通过过滤的 reads"),
    ("filtering_result.low_quality_reads", "低质量 reads"),
    ("filtering_result.too_many_N_reads", "N 过多的 reads"),
    ("filtering_result.too_short_reads", "过短 reads"),
)
THREADS_LINE = re.compile(r"^Threads per method:\s+(\d+)\s*$", re.MULTILINE)
MEMORY_LINE = re.compile(r"^Memory per method:\s+(\d+)\s+GiB\s*$", re.MULTILINE)
COMPARISON_WAITING = "比较尚未就绪。"
THRESHOLD_ABSENT = "没有达到阈值的 contig"
NO_BEST_ASSEMBLY = "不要仅根据 N50 自动推荐最佳组装结果。"
LENGTH_SCOPES = {
    "original": (
        "原始组装",
        "这次选择的组装方法的原始组装。不包含按长度筛过的 contig。",
        "指标名称与 comparison.tsv 的列一致。某个所选组装方法没有对应行时，记为无记录。",
    ),
    "gt500": (
        ">500 bp",
        "只评估长度大于 500 bp 的 contig。这一档不和原始组装放在同一张表里。",
        "数字来自这一档的 QUAST 报告。没有达到阈值时，记为「没有达到阈值的 contig」。",
    ),
    "gt1000": (
        ">1000 bp",
        "只评估长度大于 1000 bp 的 contig。这一档不和原始组装放在同一张表里。",
        "数字来自这一档的 QUAST 报告。没有达到阈值时，记为「没有达到阈值的 contig」。",
    ),
}


def selected_assemblers(raw) -> list[str] | None:
    try:
        parsed = json.loads(raw) if not isinstance(raw, list) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return None
    return parsed


def assembler_label(name: str) -> str:
    return ASSEMBLER_LABELS.get(name, name)


def parse_metric_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def bar_width(value: float, maximum: float) -> int | float:
    if maximum <= 0 or value < 0:
        return 0
    width = value / maximum * 100
    if width > 100:
        return 100
    rounded = round(width, 1)
    if float(rounded).is_integer():
        return int(rounded)
    return rounded


def _empty_comparison(message: str) -> dict:
    return {
        "ready": False,
        "message": message,
        "columns": [],
        "metrics": [],
        "missing": [],
        "missing_labels": [],
        "extra": [],
        "extra_labels": [],
        "charts": [],
        "rule": NO_BEST_ASSEMBLY,
    }


def comparison_view(
    assemblers: list[str] | None,
    rows: list[dict],
    absent_text: dict | None = None,
    waiting: str = COMPARISON_WAITING,
) -> dict:
    noted = {
        name: text
        for name, text in (absent_text or {}).items()
        if isinstance(name, str) and isinstance(text, str) and text.strip()
    }
    if assemblers is None:
        return _empty_comparison("组装器选择无法读取，比较未列出。")
    if not rows and not noted:
        return _empty_comparison(waiting)
    present = {}
    metric_order = []
    for item in rows:
        name = item.get("assembler")
        if not isinstance(name, str) or not name or name in present:
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        present[name] = metrics
        for metric in metrics:
            if metric not in metric_order:
                metric_order.append(metric)
    if not metric_order and noted:
        metric_order = list(KNOWN_METRICS)
    columns = [{"id": name, "label": assembler_label(name)} for name in assemblers]
    ordered = [metric for metric in KNOWN_METRICS if metric in metric_order]
    ordered.extend(metric for metric in metric_order if metric not in ordered)
    table = []
    for metric in ordered:
        cells = []
        for name in assemblers:
            values = present.get(name)
            if values is None:
                cells.append({"text": noted.get(name, "无记录"), "number": None})
                continue
            if metric not in values or str(values[metric]).strip() == "":
                cells.append({"text": "未记录", "number": None})
                continue
            text = str(values[metric]).strip()
            cells.append({"text": text, "number": parse_metric_number(text)})
        table.append({"name": metric, "cells": cells})
    missing = [name for name in assemblers if name not in present and name not in noted]
    extra = [name for name in present if name not in assemblers]
    charts = []
    for metric_name, title in CHART_METRICS:
        metric = next((item for item in table if item["name"] == metric_name), None)
        bars = []
        if metric is not None:
            numeric = [
                (columns[index], cell)
                for index, cell in enumerate(metric["cells"])
                if cell["number"] is not None
            ]
            maximum = max((cell["number"] for _, cell in numeric), default=0)
            for column, cell in numeric:
                bars.append(
                    {
                        "id": column["id"],
                        "label": column["label"],
                        "text": cell["text"],
                        "width": bar_width(cell["number"], maximum),
                    }
                )
        charts.append(
            {
                "metric": metric_name,
                "title": title,
                "bars": bars,
                "empty": "这项指标没有可绘制的数值。",
            }
        )
    return {
        "ready": True,
        "message": "",
        "columns": columns,
        "metrics": table,
        "missing": missing,
        "missing_labels": [assembler_label(name) for name in missing],
        "extra": extra,
        "extra_labels": [assembler_label(name) for name in extra],
        "charts": charts,
        "rule": NO_BEST_ASSEMBLY,
    }


def scope_comparison(
    scope: str,
    assemblers: list[str] | None,
    rows: list[dict],
    absent_text: dict | None = None,
    waiting: str = COMPARISON_WAITING,
) -> dict:
    view = comparison_view(assemblers, rows, absent_text=absent_text, waiting=waiting)
    label, note, caption = LENGTH_SCOPES[scope]
    view["scope"] = scope
    view["scope_label"] = label
    view["note"] = note
    view["caption"] = caption
    return view


def resource_lines(job_threads, run_log: str | None) -> dict:
    threads_match = THREADS_LINE.search(run_log or "")
    memory_match = MEMORY_LINE.search(run_log or "")
    if threads_match:
        threads = int(threads_match.group(1))
        threads_text = "本次运行记录：每种方法 {} 线程。".format(threads)
        threads_source = "run_log"
    elif isinstance(job_threads, int) and not isinstance(job_threads, bool):
        threads = job_threads
        threads_text = "任务记录的线程数：{}。运行日志还没有这次分配。".format(job_threads)
        threads_source = "job"
    else:
        threads = None
        threads_text = "线程数未记录。"
        threads_source = "absent"
    if memory_match:
        memory = int(memory_match.group(1))
        memory_text = "本次运行记录：每种方法 {} GiB。".format(memory)
        memory_source = "run_log"
    else:
        memory = None
        memory_text = "内存分配未记录。"
        memory_source = "absent"
    return {
        "threads": threads,
        "threads_source": threads_source,
        "threads_text": threads_text,
        "memory_gib": memory,
        "memory_source": memory_source,
        "memory_text": memory_text,
    }


def _dig(payload: dict, path: str):
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def format_recorded_number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 10**15:
            return str(int(value))
        return format(value, ".12g")
    return str(value)


def _side_number(payload: dict, stage: str, key: str) -> float | None:
    number = parse_metric_number(_dig(payload, "summary.{}.{}".format(stage, key)))
    raw = _dig(payload, "summary.{}.{}".format(stage, key))
    if number is None or isinstance(raw, bool):
        return None
    return number


def _rate_text(number: float) -> tuple[str, str] | None:
    if number < 0 or number > 1:
        return None
    return "{:.1f}%".format(number * 100), format_recorded_number(number)


def _bar_widths(kind: str, before: float | None, after: float | None) -> tuple[int | None, int | None]:
    if kind == "rate":
        before_width = round(before * 100) if before is not None and 0 <= before <= 1 else None
        after_width = round(after * 100) if after is not None and 0 <= after <= 1 else None
        return before_width, after_width
    if before is None or after is None or before < 0 or after < 0:
        return None, None
    scale = max(before, after)
    if scale <= 0:
        return 0, 0
    return round(before / scale * 100), round(after / scale * 100)


def _qc_side(kind: str, number: float | None) -> dict:
    if number is None:
        return {"text": None, "raw": None}
    if kind == "rate":
        shown = _rate_text(number)
        if shown is not None:
            text, raw = shown
            return {"text": text, "raw": raw}
    return {"text": format_recorded_number(number), "raw": None}


def qc_sheet(documents: list[dict]) -> dict:
    rows = []
    unreadable = []
    readable = []
    for document in documents:
        if document.get("unreadable") or not isinstance(document.get("payload"), dict):
            if document.get("present"):
                unreadable.append(document.get("name") or "fastp.json")
            continue
        readable.append(document)
    for document in readable:
        prefix = "{} · ".format(document.get("name") or "fastp.json") if len(readable) > 1 else ""
        payload = document["payload"]
        for key, label, kind in QC_PAIRS:
            before = _side_number(payload, "before_filtering", key)
            after = _side_number(payload, "after_filtering", key)
            if before is None and after is None:
                continue
            before_width, after_width = _bar_widths(kind, before, after)
            rows.append(
                {
                    "label": prefix + label,
                    "kind": kind,
                    "before": _qc_side(kind, before),
                    "after": _qc_side(kind, after),
                    "before_width": before_width,
                    "after_width": after_width,
                }
            )
    if rows and unreadable:
        note = "另有 fastp 报告无法读取：{}。".format("、".join(unreadable))
    elif rows:
        note = "数量的条只比较这一行的过滤前和过滤后。Q20、Q30、GC 的条按记录比例画出。都不表示哪一边更好。"
    elif unreadable:
        note = "质量控制指标未记录。"
    else:
        note = "质量控制指标未记录。"
    return {"ready": bool(rows), "rows": rows, "note": note}


def qc_metrics(documents: list[dict]) -> dict:
    metrics = []
    unreadable = []
    for document in documents:
        name = document.get("name") or "fastp.json"
        if document.get("unreadable") or not isinstance(document.get("payload"), dict):
            if document.get("present"):
                unreadable.append(name)
            continue
        prefix = "{} · ".format(name) if len(documents) > 1 else ""
        for path, label in FASTP_FIELDS:
            number = parse_metric_number(_dig(document["payload"], path))
            if number is None or isinstance(_dig(document["payload"], path), bool):
                continue
            metrics.append(
                {
                    "label": prefix + label,
                    "value": format_recorded_number(number),
                    "source": path,
                }
            )
    if metrics and unreadable:
        note = "另有 fastp 报告无法读取：{}。".format("、".join(unreadable))
    elif metrics:
        note = ""
    else:
        note = "质量控制指标未记录。"
    return {"metrics": metrics, "note": note, "unreadable": unreadable}


def parse_versions(text: str | None) -> list[dict]:
    if not text:
        return []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            name, version = line.split("\t", 1)
            name = name.strip()
            version = version.strip()
            if name and version:
                rows.append({"name": name, "version": version})
                continue
        rows.append({"name": "", "version": line.strip()})
    return rows


def result_sources_present(
    versions_text: str | None,
    fastp_documents: list[dict],
    comparison_rows: list[dict],
    stage_failures: list[dict],
) -> bool:
    if comparison_rows:
        return True
    if versions_text and versions_text.strip():
        return True
    if any(document.get("present") for document in fastp_documents):
        return True
    return bool(stage_failures)


def _parameter(key: str, value, checksum: str | None = None) -> dict:
    item = {"key": key, "value": value}
    if checksum:
        item["checksum"] = checksum
    return item


def assemble_report_model(
    *,
    sample_name: str,
    status_label: str,
    batch: dict | None,
    inputs: list[dict],
    assemblers: list[str] | None,
    spades_kmers: str | None,
    spades_careful: bool,
    resources: dict,
    versions_text: str | None,
    fastp_documents: list[dict],
    comparison_rows: list[dict],
    job_error: str | None,
    stage_failures: list[dict],
    comparisons: list[dict] | None = None,
) -> dict:
    record = [_parameter("样本", sample_name)]
    if batch:
        record.extend(
            (
                _parameter(
                    "测序数据 R1",
                    batch["r1_filename"],
                    batch["r1_sha256"] or "未记录校验值",
                ),
                _parameter(
                    "测序数据 R2",
                    batch["r2_filename"],
                    batch["r2_sha256"] or "未记录校验值",
                ),
            )
        )
    else:
        record.append(_parameter("测序数据", "未单独记录"))
    for item in inputs:
        if item.get("name") and item.get("bytes") is not None:
            shown = "{} · {} 字节".format(item["name"], item["bytes"])
        elif item.get("name"):
            shown = item["name"]
        else:
            shown = "文件不在"
        record.append(_parameter("任务输入 {}".format(item["role"]), shown))
    parameters = []
    if assemblers is None:
        parameters.append(_parameter("组装器", "无法读取"))
    else:
        parameters.append(_parameter("组装器", " / ".join(assemblers) if assemblers else "未记录"))
    parameters.extend(
        (
            _parameter("SPAdes k-mer", spades_kmers or "未设置"),
            _parameter("SPAdes careful", "开启" if spades_careful else "关闭"),
            _parameter("线程", resources["threads_text"]),
            _parameter("内存", resources["memory_text"]),
            _parameter("任务状态", status_label),
        )
    )
    versions = parse_versions(versions_text)
    qc = qc_metrics(fastp_documents)
    comparison = comparison_view(assemblers, comparison_rows)
    if comparisons is None:
        comparisons = [scope_comparison("original", assemblers, comparison_rows)]
    anomalies = []
    if isinstance(job_error, str) and job_error.strip():
        anomalies.append({"kind": "任务错误", "text": job_error.strip()})
    for stage in stage_failures:
        label = stage.get("label") or stage.get("id") or "阶段"
        text = str(label)
        if stage.get("id") and stage.get("id") != label:
            text = "{}（{}）".format(label, stage["id"])
        detail = stage.get("detail")
        if isinstance(detail, str) and detail.strip():
            text = "{}：{}".format(text, detail.strip())
        anomalies.append({"kind": "阶段失败", "text": text})
    return {
        "sample_name": sample_name,
        "status_label": status_label,
        "record": record,
        "parameters": parameters,
        "versions": versions,
        "versions_note": "" if versions else "软件版本未记录。",
        "qc": qc["metrics"],
        "qc_note": qc["note"],
        "qc_sheet": qc_sheet(fastp_documents),
        "comparison": comparison,
        "comparisons": comparisons,
        "anomalies": anomalies,
        "anomaly_note": "" if anomalies else "没有记录到异常。",
    }


def report_tsv(report: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(("section", "key", "assembler", "value"))
    for item in [*report.get("record", []), *report["parameters"]]:
        writer.writerow(("parameter", item["key"], "", item["value"]))
        if item.get("checksum"):
            writer.writerow(("parameter", item["key"] + " SHA-256", "", item["checksum"]))
    if report["versions"]:
        for item in report["versions"]:
            writer.writerow(("version", item["name"], "", item["version"]))
    else:
        writer.writerow(("version", "", "", report["versions_note"]))
    if report["qc"]:
        for item in report["qc"]:
            writer.writerow(("qc", item["label"], "", item["value"]))
    else:
        writer.writerow(("qc", "", "", report["qc_note"]))
    comparison = report["comparison"]
    if comparison["ready"]:
        for metric in comparison["metrics"]:
            for column, cell in zip(comparison["columns"], metric["cells"]):
                writer.writerow(("assembly", metric["name"], column["id"], cell["text"]))
        for name in comparison["missing"]:
            writer.writerow(("assembly", "", name, "无记录"))
    else:
        writer.writerow(("assembly", "", "", comparison["message"]))
    if report["anomalies"]:
        for item in report["anomalies"]:
            writer.writerow(("anomaly", item["kind"], "", item["text"]))
    else:
        writer.writerow(("anomaly", "", "", report["anomaly_note"]))
    return buffer.getvalue()
