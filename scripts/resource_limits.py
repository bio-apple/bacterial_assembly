"""Read cgroup CPU and memory limits, then fall back to the host."""

from __future__ import annotations

from pathlib import Path

UNLIMITED_MEMORY = 1 << 60


def parse_cpuset(text: str) -> int | None:
    total = 0
    for part in (text or "").replace("\n", "").split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError("cpuset range is reversed")
            total += end - start + 1
        else:
            int(piece)
            total += 1
    return total or None


def parse_cpu_quota(quota_text: str, period_text: str) -> int | None:
    quota = quota_text.strip()
    period = int(period_text.strip())
    if period < 1 or quota in {"", "max", "-1"}:
        return None
    quota_value = int(quota)
    if quota_value < 1:
        return None
    if quota_value < period:
        return 1
    return max(1, quota_value // period)


def parse_cpu_max(text: str) -> int | None:
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    return parse_cpu_quota(parts[0], parts[1])


def parse_memory_limit(text: str) -> int | None:
    token = (text or "").strip()
    if not token or token == "max":
        return None
    value = int(token)
    if value < 1 or value >= UNLIMITED_MEMORY:
        return None
    return value


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _first_text(candidates: list[Path]) -> str | None:
    for path in candidates:
        text = _read(path)
        if text is not None and text.strip():
            return text
    return None


def _relative_candidates(root: Path, controllers: str, cgroup_path: str, name: str) -> list[Path]:
    relative = cgroup_path.strip("/")
    directories = [controllers]
    pieces = [item for item in controllers.split(",") if item]
    directories.extend(item for item in pieces if item not in directories)
    found = []
    for directory in directories:
        base = root / directory
        if relative:
            found.append(base / relative / name)
        found.append(base / name)
    if relative:
        found.append(root / relative / name)
    found.append(root / name)
    return found


def combine_cpu(host_cpus: int, quota: int | None, cpuset: int | None) -> tuple[int, str]:
    host = host_cpus if host_cpus > 0 else 1
    limits = [value for value in (quota, cpuset) if isinstance(value, int) and value > 0]
    if not limits:
        return host, "host"
    return max(1, min([host, *limits])), "cgroup"


def combine_memory(host_bytes: int, limit: int | None) -> tuple[int, str]:
    if not isinstance(host_bytes, int) or host_bytes < 1:
        raise RuntimeError("system memory is unavailable")
    if not isinstance(limit, int) or limit < 1:
        return host_bytes, "host"
    return min(host_bytes, limit), "cgroup"


def read_cgroup(proc_text: str, root: Path) -> dict:
    """Return quota CPUs, cpuset CPUs, memory bytes, and which cgroup generation was read."""
    quota = None
    cpuset = None
    memory = None
    generation = None
    for line in (proc_text or "").splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, cgroup_path = parts
        if hierarchy == "0" and controllers == "":
            generation = generation or "cgroup-v2"
            quota_text = _first_text(_relative_candidates(root, "", cgroup_path, "cpu.max"))
            if quota is None and quota_text:
                try:
                    quota = parse_cpu_max(quota_text)
                except ValueError:
                    quota = None
            for name in ("cpuset.cpus.effective", "cpuset.cpus"):
                if cpuset is not None:
                    break
                cpuset_text = _first_text(_relative_candidates(root, "", cgroup_path, name))
                if not cpuset_text:
                    continue
                try:
                    cpuset = parse_cpuset(cpuset_text)
                except ValueError:
                    cpuset = None
            if memory is None:
                memory_text = _first_text(_relative_candidates(root, "", cgroup_path, "memory.max"))
                if memory_text:
                    try:
                        memory = parse_memory_limit(memory_text)
                    except ValueError:
                        memory = None
            continue
        names = set(controllers.split(","))
        if "cpu" in names and quota is None:
            generation = generation or "cgroup-v1"
            quota_text = _first_text(
                _relative_candidates(root, controllers, cgroup_path, "cpu.cfs_quota_us")
            )
            period_text = _first_text(
                _relative_candidates(root, controllers, cgroup_path, "cpu.cfs_period_us")
            )
            if quota_text and period_text:
                try:
                    quota = parse_cpu_quota(quota_text, period_text)
                except ValueError:
                    quota = None
        if "cpuset" in names and cpuset is None:
            generation = generation or "cgroup-v1"
            cpuset_text = _first_text(
                _relative_candidates(root, controllers, cgroup_path, "cpuset.cpus")
            )
            if cpuset_text:
                try:
                    cpuset = parse_cpuset(cpuset_text)
                except ValueError:
                    cpuset = None
        if "memory" in names and memory is None:
            generation = generation or "cgroup-v1"
            memory_text = _first_text(
                _relative_candidates(root, controllers, cgroup_path, "memory.limit_in_bytes")
            )
            if memory_text:
                try:
                    memory = parse_memory_limit(memory_text)
                except ValueError:
                    memory = None
    return {
        "quota_cpus": quota,
        "cpuset_cpus": cpuset,
        "memory_bytes": memory,
        "generation": generation,
    }


def limited_cpu_count(host_cpus: int) -> int:
    host = host_cpus if isinstance(host_cpus, int) and host_cpus > 0 else 1
    try:
        proc = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return host
    try:
        found = read_cgroup(proc, Path("/sys/fs/cgroup"))
        cpu, _source = combine_cpu(host, found["quota_cpus"], found["cpuset_cpus"])
    except (OSError, ValueError):
        return host
    return cpu


def limited_memory_bytes(host_bytes: int) -> int:
    try:
        proc = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
        found = read_cgroup(proc, Path("/sys/fs/cgroup"))
        total, _source = combine_memory(host_bytes, found["memory_bytes"])
    except (OSError, RuntimeError, ValueError):
        return host_bytes
    return total


def allocation_log(
    host_cpus: int,
    cpu_limit: int,
    cpu_source: str,
    memory_bytes: int,
    memory_source: str,
    threads: int,
    memory_gib: int,
) -> str:
    return (
        "Logical CPUs: {}\n"
        "CPU limit: {} ({})\n"
        "Memory limit: {} bytes ({})\n"
        "Threads per method: {}\n"
        "Memory per method: {} GiB\n"
        "fastp threads: {}\n"
        "Assembly methods: serial in selection order"
    ).format(
        host_cpus,
        cpu_limit,
        cpu_source,
        memory_bytes,
        memory_source,
        threads,
        memory_gib,
        threads,
    )
