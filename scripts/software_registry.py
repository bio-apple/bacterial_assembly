"""Load the bioinformatics tool image whitelist."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = ROOT / "config" / "software_registry.yaml"
JOB_LABEL = "bacterial.assembly.job_id"
TOOL_LABEL = "bacterial.assembly.tool"


def _parse_yaml_tools(text: str) -> dict[str, dict]:
    """Minimal YAML subset parser for this registry (no PyYAML dependency)."""
    tools: dict[str, dict] = {}
    current: str | None = None
    in_executables = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("tools:"):
            continue
        if line[:2] == "  " and not line.startswith("    ") and line.strip().endswith(":"):
            current = line.strip().rstrip(":")
            tools[current] = {"image": "", "executables": []}
            in_executables = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("image:"):
            tools[current]["image"] = stripped.split(":", 1)[1].strip()
            in_executables = False
            continue
        if stripped == "executables:":
            in_executables = True
            continue
        if in_executables and stripped.startswith("- "):
            tools[current]["executables"].append(stripped[2:].strip())
    return tools


@lru_cache(maxsize=4)
def load_registry(path: str | None = None) -> dict[str, dict]:
    registry_path = Path(path) if path else DEFAULT_REGISTRY
    text = registry_path.read_text(encoding="utf-8")
    tools = _parse_yaml_tools(text)
    if not tools:
        raise RuntimeError("software registry has no tools: {}".format(registry_path))
    for name, entry in tools.items():
        if not entry.get("image") or not entry.get("executables"):
            raise RuntimeError("incomplete registry entry for {}".format(name))
    return tools


def clear_registry_cache() -> None:
    load_registry.cache_clear()


def executable_name(command: Sequence[str]) -> str | None:
    if not command:
        return None
    # micromamba run -n unicycler unicycler ...
    if (
        len(command) >= 5
        and Path(command[0]).name == "micromamba"
        and command[1] == "run"
        and command[2] == "-n"
        and command[3] == "unicycler"
    ):
        return Path(command[4]).name
    return Path(command[0]).name


def resolve_tool(command: Sequence[str], registry: dict[str, dict] | None = None) -> str | None:
    exe = executable_name(command)
    if exe is None:
        return None
    tools = registry if registry is not None else load_registry()
    for name, entry in tools.items():
        if exe in entry["executables"]:
            return name
    return None


def docker_argv(command: Sequence[str]) -> list[str]:
    """Strip micromamba wrappers; container images already have tools on PATH."""
    if (
        len(command) >= 5
        and Path(command[0]).name == "micromamba"
        and command[1] == "run"
        and command[2] == "-n"
        and command[3] == "unicycler"
    ):
        return [str(item) for item in command[4:]]
    return [str(item) for item in command]
