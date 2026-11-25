#!/usr/bin/env python3
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scripts.stage_record import load_stage_record
from web.steps import sync_job_steps
from web.app import append_job_log, contained_path, create_app
from web.errors import WORKER_INTERRUPTED, explain_job_failure
from web.notices import note_disk, note_job, volume_is_low


@contextmanager
def connect(database: Path):
    db = sqlite3.connect(database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


TERMINAL_STATUSES = ("succeeded", "partial_success", "failed", "cancelled")
ALLOWED_TRANSITIONS = {
    "running": ("queued",),
    "cancelling": ("running",),
    "succeeded": ("running",),
    "partial_success": ("running",),
    "failed": ("running",),
    "cancelled": ("queued", "running", "cancelling"),
}
STATUS_LINE = re.compile(
    r"^STATUS: (queued|running|cancelling|cancelled|succeeded|partial_success|failed)\s*$",
    re.MULTILINE,
)


def worker_identity() -> str:
    return "{}:{}".format(socket.gethostname(), os.getpid())


TERM_GRACE_SECONDS = 5.0
FINAL_CONTIGS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}
INCOMPLETE_TEMP_DIRS = {
    "spades": ("tmp", "corrected", "misc"),
    "unicycler": ("spades_output", "read_correction", "pilon_polish"),
    "shovill": ("tmp",),
    "megahit": ("tmp", "intermediate_contigs"),
}


def signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def stop_process_group(process: subprocess.Popen, grace_seconds: float = TERM_GRACE_SECONDS) -> int | None:
    if process.poll() is not None:
        return process.returncode
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        try:
            return process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return process.poll()
    signal_group(pgid, signal.SIGTERM)
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_group(pgid, signal.SIGKILL)
        try:
            return process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return process.poll()


def stop_pid_group(pid: int, grace_seconds: float = TERM_GRACE_SECONDS) -> None:
    try:
        os.kill(pid, 0)
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid != pid or pgid == os.getpgrp() or pgid <= 1:
        return
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    signal_group(pgid, signal.SIGKILL)


def command_belongs_to_job(command: str, job_id: str) -> bool:
    try:
        token = str(uuid.UUID(str(job_id)))
    except ValueError:
        return False
    if "--job-id {}".format(token) in command or "--job-id={}".format(token) in command:
        return True
    return "/{}/".format(token) in command.replace("\\", "/")


def read_process(pid: int) -> dict | None:
    if pid < 1:
        return None
    stat_path = Path("/proc/{}/stat".format(pid))
    if stat_path.is_file():
        return _read_linux_process(pid)
    return _read_ps_process(pid)


def _read_linux_process(pid: int) -> dict | None:
    try:
        stat = Path("/proc/{}/stat".format(pid)).read_text(encoding="utf-8")
        command = (
            Path("/proc/{}/cmdline".format(pid))
            .read_bytes()
            .replace(b"\x00", b" ")
            .decode("utf-8", "replace")
            .strip()
        )
    except OSError:
        return None
    end = stat.rfind(")")
    if end < 0:
        return None
    fields = stat[end + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "pid": pid,
            "pgid": int(fields[2]),
            "start": fields[19],
            "command": command,
        }
    except ValueError:
        return None


def _read_ps_process(pid: int) -> dict | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "pgid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    pgid_text, _, rest = line.partition(" ")
    rest = rest.strip()
    try:
        pgid = int(pgid_text)
    except ValueError:
        return None
    parts = rest.split(None, 5)
    if len(parts) < 6:
        return None
    return {
        "pid": pid,
        "pgid": pgid,
        "start": " ".join(parts[:5]),
        "command": parts[5],
    }


def list_processes() -> list[dict]:
    if Path("/proc").is_dir():
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                snap = _read_linux_process(int(entry.name))
                if snap:
                    found.append(snap)
        return found
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            found.append(
                {"pid": int(parts[0]), "pgid": int(parts[1]), "start": None, "command": parts[2]}
            )
        except ValueError:
            continue
    return found


def leader_matches(pid: int, expected_pgid: int | None, expected_start: str | None, job_id: str) -> dict | None:
    snap = read_process(pid)
    if snap is None or snap["pgid"] != snap["pid"] or snap["pgid"] <= 1:
        return None
    if expected_pgid is not None and snap["pgid"] != int(expected_pgid):
        return None
    if expected_start and snap["start"] != expected_start:
        return None
    if not command_belongs_to_job(snap["command"], job_id):
        return None
    return snap


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return


def _job_processes_alive(groups: set[int], individuals: list[int], job_id: str) -> bool:
    current = os.getpid()
    for snap in list_processes():
        if snap["pid"] == current or not command_belongs_to_job(snap["command"], job_id):
            continue
        if snap["pgid"] in groups or snap["pid"] in individuals:
            return True
    return False


def stop_job_processes(
    job_id: str,
    pid: int | None,
    pgid: int | None,
    started: str | None,
    grace_seconds: float = TERM_GRACE_SECONDS,
) -> bool:
    """Signal this job's processes only after the group, start time, and job id agree."""
    current = os.getpid()
    own_group = os.getpgrp()
    leader = leader_matches(int(pid), pgid, started, job_id) if pid else None
    processes = list_processes()
    matching = [
        item
        for item in processes
        if item["pid"] != current and command_belongs_to_job(item["command"], job_id)
    ]
    by_group: dict[int, list[dict]] = {}
    for item in processes:
        by_group.setdefault(item["pgid"], []).append(item)
    groups: set[int] = set()
    individuals: list[int] = []
    if leader and leader["pgid"] != own_group:
        groups.add(leader["pgid"])
    for item in matching:
        if item["pgid"] == own_group or item["pgid"] <= 1:
            continue
        members = by_group.get(item["pgid"], [])
        foreign = [
            member
            for member in members
            if member["pid"] != current and not command_belongs_to_job(member["command"], job_id)
        ]
        if foreign:
            individuals.append(item["pid"])
        else:
            groups.add(item["pgid"])
    if not groups and not individuals:
        return False
    for group in groups:
        signal_group(group, signal.SIGTERM)
    for item_pid in individuals:
        _signal_pid(item_pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _job_processes_alive(groups, individuals, job_id):
            return True
        time.sleep(0.05)
    for group in groups:
        signal_group(group, signal.SIGKILL)
    for item_pid in individuals:
        _signal_pid(item_pid, signal.SIGKILL)
    return True


def remove_incomplete_tool_temp(pipeline_output_dir: Path) -> None:
    assemblies = pipeline_output_dir / "assemblies"
    if not assemblies.is_dir():
        return
    root = assemblies.resolve()
    for method, final_name in FINAL_CONTIGS.items():
        method_dir = assemblies / method
        if not method_dir.is_dir() or method_dir.is_symlink():
            continue
        if (method_dir / final_name).is_file():
            continue
        for name in INCOMPLETE_TEMP_DIRS[method]:
            target = method_dir / name
            if not target.is_dir() or target.is_symlink():
                continue
            resolved = target.resolve()
            if root not in resolved.parents:
                continue
            shutil.rmtree(resolved)


def cancellation_requested(database: Path, job_id: str) -> bool:
    with connect(database) as db:
        row = db.execute(
            "SELECT cancel_requested, status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelling"))


def mark_cancelling(database: Path, job_id: str) -> None:
    timestamp = now()
    with connect(database) as db:
        updated = db.execute(
            "UPDATE jobs SET status = 'cancelling', cancel_requested_at = COALESCE(cancel_requested_at, ?), "
            "updated_at = ? WHERE id = ? AND status = 'running'",
            (timestamp, timestamp, job_id),
        )
        if updated.rowcount != 1:
            return
        row = db.execute(
            "SELECT owner, sample FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row:
            note_job(db, row["owner"], row["sample"], job_id, "cancelling")


def remember_process(database: Path, job_id: str, process: subprocess.Popen) -> tuple[int | None, str | None]:
    snap = read_process(process.pid)
    pgid = None
    started = None
    if snap and snap["pgid"] == process.pid and command_belongs_to_job(snap["command"], job_id):
        pgid = snap["pgid"]
        started = snap["start"]
    else:
        try:
            current_group = os.getpgid(process.pid)
        except ProcessLookupError:
            current_group = None
        if current_group == process.pid:
            pgid = current_group
    with connect(database) as db:
        db.execute(
            "UPDATE jobs SET worker_pid = ?, process_group_id = ?, process_started_at = ?, "
            "updated_at = ? WHERE id = ? AND status IN ('running', 'cancelling')",
            (process.pid, pgid, started, now(), job_id),
        )
    return pgid, started


def finalize_cancelled(database: Path, job_id: str, log_path: Path | None, pipeline_output_dir: Path | None) -> None:
    if pipeline_output_dir is not None:
        try:
            remove_incomplete_tool_temp(pipeline_output_dir)
        except OSError as error:
            if log_path is not None:
                append_job_log(log_path, "Could not remove incomplete tool temp: {}".format(error))
    with connect(database) as db:
        row = db.execute(
            "SELECT owner, sample FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        timestamp = now()
        updated = db.execute(
            "UPDATE jobs SET status = 'cancelled', error = NULL, worker_pid = NULL, "
            "process_group_id = NULL, process_started_at = NULL, "
            "cancel_requested_at = COALESCE(cancel_requested_at, ?), "
            "updated_at = ?, completed_at = ? "
            "WHERE id = ? AND status IN ('running', 'cancelling')",
            (timestamp, timestamp, timestamp, job_id),
        )
        changed = updated.rowcount == 1
        if changed and row:
            note_job(db, row["owner"], row["sample"], job_id, "cancelled")
    if changed and log_path is not None:
        append_job_log(log_path, "STATUS: cancelled")


def last_logged_status(log_path: str | None) -> str | None:
    if not log_path:
        return None
    found = STATUS_LINE.findall(_log_text(Path(log_path)))
    return found[-1] if found else None


def recover_interrupted_jobs(database: Path) -> None:
    with connect(database) as db:
        rows = db.execute(
            "SELECT id, owner, sample, status, cancel_requested, worker_pid, "
            "process_group_id, process_started_at, log_path FROM jobs "
            "WHERE status IN ('running', 'cancelling')"
        ).fetchall()
    for row in rows:
        signalled = stop_job_processes(
            row["id"],
            int(row["worker_pid"]) if row["worker_pid"] else None,
            int(row["process_group_id"]) if row["process_group_id"] else None,
            row["process_started_at"],
            grace_seconds=2,
        )
        if row["worker_pid"] and not signalled:
            append_job_log(
                row["log_path"],
                "RECONCILE: stored process did not match this job; did not signal it",
            )
    timestamp = now()
    with connect(database) as db:
        for row in rows:
            logged = last_logged_status(row["log_path"])
            cancel = (
                row["status"] == "cancelling"
                or row["cancel_requested"]
                or logged == "cancelled"
            )
            if cancel:
                updated = db.execute(
                    "UPDATE jobs SET status = 'cancelled', worker_pid = NULL, "
                    "process_group_id = NULL, process_started_at = NULL, "
                    "cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                    "updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status IN ('running', 'cancelling')",
                    (timestamp, timestamp, timestamp, row["id"]),
                )
                if updated.rowcount == 1:
                    note_job(db, row["owner"], row["sample"], row["id"], "cancelled")
                if logged != "cancelled":
                    append_job_log(row["log_path"], "STATUS: cancelled")
            else:
                updated = db.execute(
                    "UPDATE jobs SET status = 'failed', worker_pid = NULL, "
                    "process_group_id = NULL, process_started_at = NULL, "
                    "error = ?, updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (WORKER_INTERRUPTED, timestamp, timestamp, row["id"]),
                )
                if updated.rowcount == 1:
                    note_job(db, row["owner"], row["sample"], row["id"], "failed")
                    append_job_log(
                        row["log_path"],
                        "STATUS: failed\nRECONCILE: worker stopped before the database reached a terminal status",
                    )
    reconcile_terminal_logs(database)
    for row in rows:
        persist_job_steps(database, row["id"])


def reconcile_terminal_logs(database: Path) -> None:
    with connect(database) as db:
        rows = db.execute(
            "SELECT status, log_path FROM jobs WHERE status IN ('succeeded', 'partial_success', 'failed', 'cancelled')"
        ).fetchall()
    for row in rows:
        if last_logged_status(row["log_path"]) == row["status"]:
            continue
        append_job_log(
            row["log_path"],
            "STATUS: {}\nRECONCILE: kept database status".format(row["status"]),
        )


def persist_job_steps(database: Path, job_id: str) -> None:
    try:
        _persist_job_steps(database, job_id)
    except (OSError, sqlite3.Error, ValueError) as error:
        print("Could not record steps for {}: {}".format(job_id, error), file=sys.stderr)


def _persist_job_steps(database: Path, job_id: str) -> None:
    with connect(database) as db:
        row = db.execute(
            "SELECT assemblers, status, output_dir FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return
    try:
        assemblers = json.loads(row["assemblers"])
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(assemblers, list):
        return
    output = Path(row["output_dir"])
    record = {"stages": []}
    for candidate in (
        output.parent / "work" / "stages.json",
        output / "stages.json",
        output / "outputs" / "stages.json",
    ):
        loaded = load_stage_record(candidate)
        if loaded.get("stages"):
            record = loaded
            break
    with connect(database) as db:
        sync_job_steps(db, job_id, assemblers, record, row["status"])


def claim_next_job(database: Path):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        timestamp = now()
        pending = db.execute(
            "SELECT id, owner, sample, log_path FROM jobs "
            "WHERE status = 'queued' AND cancel_requested = 1"
        ).fetchall()
        for row in pending:
            updated = db.execute(
                "UPDATE jobs SET status = 'cancelled', "
                "cancel_requested_at = COALESCE(cancel_requested_at, ?), "
                "updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (timestamp, timestamp, timestamp, row["id"]),
            )
            if updated.rowcount == 1:
                note_job(db, row["owner"], row["sample"], row["id"], "cancelled")
            append_job_log(row["log_path"], "STATUS: cancelled")
        job = db.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND cancel_requested = 0 "
            "ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        if job is None:
            db.commit()
            return None
        updated = db.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?, "
            "attempt = attempt + 1, heartbeat_at = ?, worker_id = ? "
            "WHERE id = ? AND status = 'queued' AND cancel_requested = 0",
            (timestamp, timestamp, timestamp, worker_identity(), job["id"]),
        )
        if updated.rowcount == 1:
            note_job(db, job["owner"], job["sample"], job["id"], "running")
        db.commit()
        if updated.rowcount != 1:
            return None
        return job


def transition_job(database: Path, job_id: str, status: str, expect: tuple[str, ...], **fields) -> bool:
    allowed = ALLOWED_TRANSITIONS.get(status)
    if allowed is None or not expect or any(item not in allowed for item in expect):
        raise ValueError("illegal job transition to {}".format(status))
    if status not in TERMINAL_STATUSES and ("completed_at" in fields or "archived_at" in fields):
        raise ValueError("only a finished job records an end or archive time")
    timestamp = now()
    fields = {
        key: value
        for key, value in fields.items()
        if key not in {"status", "completed_at", "id"}
    }
    fields["status"] = status
    fields["updated_at"] = timestamp
    if status in TERMINAL_STATUSES:
        fields["completed_at"] = timestamp
        fields["worker_pid"] = None
        fields["process_group_id"] = None
        fields["process_started_at"] = None
    if fields.get("archive_path"):
        fields["archived_at"] = timestamp
    assignments = ", ".join("{} = ?".format(key) for key in fields)
    placeholders = ", ".join("?" for _ in expect)
    with connect(database) as db:
        updated = db.execute(
            "UPDATE jobs SET {} WHERE id = ? AND status IN ({})".format(assignments, placeholders),
            (*fields.values(), job_id, *expect),
        )
        if updated.rowcount != 1:
            return False
        if status in ("succeeded", "partial_success", "failed", "cancelled", "cancelling", "running"):
            row = db.execute(
                "SELECT owner, sample FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row and status in ("succeeded", "partial_success", "failed", "cancelled"):
                note_job(db, row["owner"], row["sample"], job_id, status)
        return True


def record_heartbeat(database: Path, job_id: str) -> None:
    with connect(database) as db:
        db.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status IN ('running', 'cancelling')",
            (now(), job_id),
        )


def update_job(database: Path, job_id: str, status: str, **fields) -> bool:
    return transition_job(database, job_id, status, ("running",), **fields)


def publish_directory(source: Path, destination: Path) -> None:
    """Move a finished work directory into place only after every child has been staged."""
    staging = destination.parent / "{}.incoming".format(destination.name)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for child in list(source.iterdir()):
            target = staging / child.name
            try:
                child.replace(target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.move(str(child), str(target))
        if destination.exists():
            if any(destination.iterdir()):
                raise RuntimeError("output directory is not empty")
            destination.rmdir()
        staging.rename(destination)
    except Exception:
        if not destination.exists():
            destination.mkdir(parents=True, exist_ok=True)
        raise


def remember_low_disk(app, job) -> None:
    try:
        low = volume_is_low(app.config["DATA_ROOT"])
    except OSError:
        return
    if not low:
        return
    with connect(app.config["DATABASE"]) as db:
        note_disk(db, job["owner"], job["sample"], job["id"])


def _log_text(log_path: Path | None) -> str:
    if log_path is None or not log_path.is_file():
        return ""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _stages(pipeline_output_dir: Path | None):
    if pipeline_output_dir is None:
        return []
    return load_stage_record(pipeline_output_dir / "stages.json").get("stages") or []


def build_pipeline_command(
    pipeline,
    job_id: str,
    r1,
    r2,
    outdir,
    sample: str,
    assemblers: list[str],
    spades_kmers: str | None,
    spades_careful: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(pipeline),
        "--r1",
        str(r1),
        "--r2",
        str(r2),
        "--outdir",
        str(outdir),
        "--sample",
        sample,
        "--job-id",
        str(uuid.UUID(str(job_id))),
    ]
    for assembler in assemblers:
        command.extend(["--assembler", assembler])
    if "spades" in assemblers and spades_kmers:
        command.extend(["--spades-kmers", spades_kmers])
    if "spades" in assemblers and spades_careful:
        command.append("--spades-careful")
    return command


ASSEMBLER_OUTPUTS = {
    "spades": "contigs.fasta",
    "unicycler": "assembly.fasta",
    "shovill": "contigs.fa",
    "megahit": "final.contigs.fa",
}


def read_outcome(pipeline_output_dir: Path | None):
    if pipeline_output_dir is None:
        return None
    path = pipeline_output_dir / "assembly_outcome.json"
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if payload.get("status") not in {"succeeded", "partial_success", "failed"}:
        return None
    if type(payload.get("exit_code")) is not int:
        return None
    for key in ("selected_assemblers", "successful_assemblers", "failed_assemblers"):
        value = payload.get(key)
        if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
            return None
    return payload


def assembler_output_present(pipeline_output_dir: Path, assembler: str) -> bool:
    filename = ASSEMBLER_OUTPUTS.get(assembler)
    if filename is None:
        return False
    path = pipeline_output_dir / "assemblies" / assembler / filename
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def finished_status(returncode, pipeline_output_dir: Path | None) -> str:
    payload = read_outcome(pipeline_output_dir)
    if payload is None or returncode != payload["exit_code"] or pipeline_output_dir is None:
        return "failed"
    selected = payload["selected_assemblers"]
    successful = payload["successful_assemblers"]
    failed = payload["failed_assemblers"]
    if set(successful) & set(failed):
        return "failed"
    if not set(successful).issubset(selected) or not set(failed).issubset(selected):
        return "failed"
    if any(not assembler_output_present(pipeline_output_dir, name) for name in successful):
        return "failed"
    status = payload["status"]
    exit_code = payload["exit_code"]
    if status == "succeeded" and exit_code == 0 and successful == selected and not failed:
        return "succeeded"
    if status == "partial_success" and exit_code == 3 and successful and failed:
        return "partial_success"
    return "failed"


def failure_message(returncode, error, log_path, pipeline_output_dir, partial: bool) -> str:
    _code, message = explain_job_failure(
        returncode,
        error,
        _log_text(log_path),
        _stages(pipeline_output_dir),
        partial,
    )
    return message


def process_next_job(app) -> bool:
    job = claim_next_job(app.config["DATABASE"])
    if job is None:
        return False

    data_root = app.config["DATA_ROOT"]
    log_path = None
    pipeline_output_dir = None
    cancelled = False
    returncode = None
    unfinished_archive = None
    remember_low_disk(app, job)
    try:
        output_dir = contained_path(data_root, job["output_dir"])
        log_path = contained_path(data_root, job["log_path"])
        input_r1 = contained_path(data_root, job["input_r1"])
        input_r2 = contained_path(data_root, job["input_r2"])
        job_id = str(uuid.UUID(str(job["id"])))
        work_dir = output_dir.parent / "work"
        publish = output_dir.name == "output" and work_dir.is_dir()
        pipeline_output_dir = work_dir if publish else output_dir / "outputs"
        if not publish:
            pipeline_output_dir.mkdir(parents=True, exist_ok=True)
        assemblers = json.loads(job["assemblers"])
        command = build_pipeline_command(
            app.config["PIPELINE_PATH"],
            job_id,
            input_r1,
            input_r2,
            pipeline_output_dir,
            job["sample"],
            assemblers,
            job["spades_kmers"],
            bool(job["spades_careful"]),
        )

        returncode = None
        group_id = None
        started_at = None
        with log_path.open("a", encoding="utf-8") as log_file:
            if cancellation_requested(app.config["DATABASE"], job["id"]):
                mark_cancelling(app.config["DATABASE"], job["id"])
                log_file.write("STATUS: cancelling\n")
                log_file.flush()
                cancelled = True
            else:
                log_file.write(
                    "Web job {}\nSTATUS: running\nCOMMAND: {}\n".format(job["id"], command)
                )
                log_file.flush()
                process = subprocess.Popen(
                    command,
                    cwd=Path(app.config["PIPELINE_PATH"]).resolve().parent,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    start_new_session=True,
                )
                group_id, started_at = remember_process(app.config["DATABASE"], job["id"], process)
                record_heartbeat(app.config["DATABASE"], job["id"])
                next_heartbeat = time.monotonic() + 5
                while process.poll() is None:
                    if time.monotonic() >= next_heartbeat:
                        record_heartbeat(app.config["DATABASE"], job["id"])
                        next_heartbeat = time.monotonic() + 5
                    if cancellation_requested(app.config["DATABASE"], job["id"]):
                        mark_cancelling(app.config["DATABASE"], job["id"])
                        log_file.write("STATUS: cancelling\n")
                        log_file.flush()
                        stop_process_group(process)
                        stop_job_processes(
                            job["id"], process.pid, group_id, started_at, grace_seconds=1
                        )
                        cancelled = True
                        break
                    time.sleep(0.2)
                returncode = process.returncode
        if cancelled or (
            returncode not in (0, None) and cancellation_requested(app.config["DATABASE"], job["id"])
        ):
            if not cancelled:
                mark_cancelling(app.config["DATABASE"], job["id"])
            finalize_cancelled(
                app.config["DATABASE"], job["id"], log_path, pipeline_output_dir
            )
            persist_job_steps(app.config["DATABASE"], job["id"])
            return True

        verified = finished_status(returncode, pipeline_output_dir)
        comparison_present = (pipeline_output_dir / "comparison.tsv").is_file()
        if returncode and verified == "failed" and not comparison_present:
            raise RuntimeError("Pipeline exited with status {}".format(returncode))

        archived_dir = pipeline_output_dir
        if publish:
            publish_directory(pipeline_output_dir, output_dir)
            archived_dir = output_dir
        archive_path = contained_path(
            data_root, app.config["ARCHIVE_ROOT"] / "{}.tar.gz".format(job_id)
        )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        partial_archive = archive_path.with_name(archive_path.name + ".partial")
        unfinished_archive = partial_archive
        partial_archive.unlink(missing_ok=True)
        with tarfile.open(partial_archive, "w:gz") as archive:
            archive.add(
                archived_dir,
                arcname="results",
                recursive=True,
                filter=lambda member: None if member.issym() else member,
            )
        if not partial_archive.is_file() or partial_archive.stat().st_size < 1:
            partial_archive.unlink(missing_ok=True)
            raise RuntimeError("archive was not written")
        partial_archive.replace(archive_path)
        unfinished_archive = None
        final_status = finished_status(returncode, archived_dir)
        published = update_job(
            app.config["DATABASE"],
            job["id"],
            final_status,
            archive_path=str(archive_path),
            worker_pid=None,
            error=(
                None
                if final_status == "succeeded"
                else failure_message(
                    returncode,
                    None,
                    log_path,
                    pipeline_output_dir,
                    final_status == "partial_success"
                    or (
                        final_status == "failed"
                        and (archived_dir / "comparison.tsv").is_file()
                    ),
                )
            ),
        )
        if not published:
            archive_path.unlink(missing_ok=True)
            if cancellation_requested(app.config["DATABASE"], job["id"]):
                finalize_cancelled(
                    app.config["DATABASE"], job["id"], log_path, pipeline_output_dir
                )
    except Exception as error:
        if unfinished_archive is not None:
            try:
                unfinished_archive.unlink(missing_ok=True)
            except OSError:
                pass
        if cancelled or cancellation_requested(app.config["DATABASE"], job["id"]):
            finalize_cancelled(
                app.config["DATABASE"], job["id"], log_path, pipeline_output_dir
            )
            persist_job_steps(app.config["DATABASE"], job["id"])
            return True
        try:
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write("\nWEB WORKER ERROR:\n{}\n".format(traceback.format_exc()))
        except OSError as log_error:
            print("Could not append job error to {}: {}".format(log_path, log_error), file=sys.stderr)
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            try:
                with connect(app.config["DATABASE"]) as db:
                    note_disk(db, job["owner"], job["sample"], job["id"])
            except sqlite3.Error:
                print("Could not record disk notice for {}".format(job["id"]), file=sys.stderr)
        update_job(
            app.config["DATABASE"],
            job["id"],
            "failed",
            worker_pid=None,
            error=failure_message(returncode, error, log_path, pipeline_output_dir, False),
        )
    persist_job_steps(app.config["DATABASE"], job["id"])
    return True


def main() -> int:
    app = create_app()
    lock_path = app.config["DATA_ROOT"] / "worker.lock"
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another job worker already holds the queue lock.", file=sys.stderr)
            return 1
        recover_interrupted_jobs(app.config["DATABASE"])
        print("Serial job worker started.", flush=True)
        while True:
            if not process_next_job(app):
                time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
