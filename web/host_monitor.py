import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_CPU_LOCK = threading.Lock()
_CPU_SAMPLE = {"total": None, "idle": None}


def snapshot(data_root: Path) -> dict:
    return {
        "cpu": _read_metric(_cpu_usage),
        "memory": _read_metric(_memory_usage),
        "disk": _read_metric(lambda: _disk_usage(data_root)),
    }


def _read_metric(reader):
    try:
        value = reader()
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    value["ok"] = True
    return value


def _cpu_usage() -> dict:
    total, idle = _cpu_counters()
    with _CPU_LOCK:
        previous_total = _CPU_SAMPLE["total"]
        previous_idle = _CPU_SAMPLE["idle"]
        _CPU_SAMPLE["total"] = total
        _CPU_SAMPLE["idle"] = idle
    if previous_total is None or total <= previous_total:
        time.sleep(0.05)
        total, idle = _cpu_counters()
        with _CPU_LOCK:
            previous_total = _CPU_SAMPLE["total"]
            previous_idle = _CPU_SAMPLE["idle"]
            _CPU_SAMPLE["total"] = total
            _CPU_SAMPLE["idle"] = idle
    delta_total = total - previous_total
    delta_idle = idle - previous_idle
    if delta_total <= 0:
        raise RuntimeError("could not sample CPU")
    percent = (1 - (delta_idle / delta_total)) * 100
    return {
        "percent": round(min(100.0, max(0.0, percent)), 1),
        "cores": os.cpu_count() or 1,
    }


def _cpu_counters() -> tuple[int, int]:
    stat = Path("/proc/stat")
    if stat.is_file():
        ticks = [int(item) for item in stat.read_text(encoding="utf-8").splitlines()[0].split()[1:]]
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
        return sum(ticks), idle
    if sys.platform == "darwin":
        return _darwin_cpu_counters()
    raise RuntimeError("could not read CPU")


def _darwin_cpu_counters() -> tuple[int, int]:
    class HostCpuLoadInfo(ctypes.Structure):
        _fields_ = [("cpu_ticks", ctypes.c_uint * 4)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    count = ctypes.c_uint(4)
    info = HostCpuLoadInfo()
    result = libc.host_statistics(
        libc.mach_host_self(),
        3,
        ctypes.byref(info),
        ctypes.byref(count),
    )
    if result != 0:
        raise RuntimeError("could not read CPU")
    user, system, idle, nice = info.cpu_ticks
    return user + system + idle + nice, idle


def _memory_usage() -> dict:
    if Path("/proc/meminfo").is_file():
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, rest = line.split(":", 1)
            values[key] = int(rest.split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values["MemFree"])
    elif sys.platform == "darwin":
        total, available = _darwin_memory()
    else:
        raise RuntimeError("could not read memory")
    if total < 1:
        raise RuntimeError("could not read memory")
    used = max(0, total - min(total, available))
    return {
        "used_bytes": used,
        "total_bytes": total,
        "percent": round(used / total * 100, 1),
    }


def _darwin_memory() -> tuple[int, int]:
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = os.sysconf("SC_PHYS_PAGES") * page_size
    result = subprocess.run(
        ["vm_stat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not read memory")
    pages = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        number = value.strip().strip(".")
        if number.isdigit():
            pages[key.strip()] = int(number)
    if not pages:
        raise RuntimeError("could not read memory")
    available = (
        pages.get("Pages free", 0)
        + pages.get("Pages inactive", 0)
        + pages.get("Pages speculative", 0)
    ) * page_size
    return total, available


def _disk_usage(data_root: Path) -> dict:
    path = Path(data_root)
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    if usage.total < 1:
        raise RuntimeError("could not read disk")
    return {
        "used_bytes": usage.used,
        "total_bytes": usage.total,
        "percent": round(usage.used / usage.total * 100, 1),
        "path": str(path),
    }


def format_bytes(amount: int) -> str:
    value = float(amount)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return "{} B".format(int(value))
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} TiB".format(value)
