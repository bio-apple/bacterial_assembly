"""分析任务方法级 CPU/内存预算：主机与 cgroup 取紧后 90%。"""

from __future__ import annotations

from pathlib import Path

from scripts.common import (
    cpu_share,
    logical_cpu_count,
    memory_share_gib,
    physical_memory_bytes,
)
from scripts.resource_limits import allocation_log, combine_cpu, combine_memory, read_cgroup


def read_runtime_limits(
    cgroup_proc: Path | None = None,
    cgroup_root: Path | None = None,
) -> dict:
    cpu_count = logical_cpu_count()
    proc = cgroup_proc or Path("/proc/self/cgroup")
    root = cgroup_root or Path("/sys/fs/cgroup")
    try:
        detected = read_cgroup(
            proc.read_text(encoding="utf-8", errors="replace"),
            root,
        )
    except OSError:
        detected = {
            "quota_cpus": None,
            "cpuset_cpus": None,
            "memory_bytes": None,
            "generation": None,
        }
    cpu_limit, cpu_source = combine_cpu(
        cpu_count, detected["quota_cpus"], detected["cpuset_cpus"]
    )
    if detected["generation"] and cpu_source == "cgroup":
        cpu_source = detected["generation"]
    memory_limit, memory_source = combine_memory(
        physical_memory_bytes(), detected["memory_bytes"]
    )
    if detected["generation"] and memory_source == "cgroup":
        memory_source = detected["generation"]
    threads = cpu_share(cpu_limit)
    memory_gib = memory_share_gib(memory_limit)
    return {
        "host_cpus": cpu_count,
        "cpu_limit": cpu_limit,
        "cpu_source": cpu_source,
        "memory_limit_bytes": memory_limit,
        "memory_source": memory_source,
        "method_threads": threads,
        "method_memory_gib": memory_gib,
        "allocation_message": allocation_log(
            cpu_count,
            cpu_limit,
            cpu_source,
            memory_limit,
            memory_source,
            threads,
            memory_gib,
        ),
    }
