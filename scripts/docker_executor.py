"""Run whitelisted bioinformatics tools inside Docker containers."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Sequence

try:
    from scripts.software_registry import (
        JOB_LABEL,
        TOOL_LABEL,
        docker_argv,
        load_registry,
        resolve_tool,
    )
except ImportError:
    from software_registry import (  # type: ignore
        JOB_LABEL,
        TOOL_LABEL,
        docker_argv,
        load_registry,
        resolve_tool,
    )

JOB_PATH_RE = re.compile(r"(.*/jobs/[0-9a-fA-F-]{36})(?:/|$)")


def tool_runtime() -> str:
    value = os.environ.get("TOOL_RUNTIME", "local").strip().lower()
    if value in {"", "local"}:
        return "local"
    if value == "docker":
        return "docker"
    raise RuntimeError("TOOL_RUNTIME must be 'docker' or 'local', got {!r}".format(value))


def _memory_limit_bytes() -> int:
    assigned = os.environ.get("_ASSEMBLY_METHOD_MEMORY_GIB")
    if assigned is not None:
        gib = int(assigned)
    else:
        try:
            from scripts.common import memory_share_gib
        except ImportError:
            from common import memory_share_gib

        gib = memory_share_gib()
    if gib < 1:
        raise RuntimeError("method memory quota must be at least 1 GiB")
    return (gib + 1) * (1024 ** 3)


def _cpu_nano() -> int:
    assigned = os.environ.get("_ASSEMBLY_ASSIGNED_THREADS")
    if assigned is not None:
        threads = int(assigned)
    else:
        try:
            from scripts.common import cpu_share
        except ImportError:
            from common import cpu_share

        threads = cpu_share()
    if threads < 1:
        raise RuntimeError("thread quota must be positive")
    return threads * 1_000_000_000


def _job_id() -> str | None:
    value = os.environ.get("_ASSEMBLY_JOB_ID", "").strip()
    return value or None


def _job_cancel_requested(job_id: str) -> bool:
    try:
        from web.step_store import job_cancel_requested

        return job_cancel_requested(job_id)
    except Exception:
        return False


def mount_roots_for(command: Sequence[str]) -> list[Path]:
    roots: set[Path] = set()
    data_root = os.environ.get("ASSEMBLY_DATA_ROOT", "").strip()
    for arg in command:
        text = str(arg)
        if not text.startswith("/"):
            continue
        match = JOB_PATH_RE.match(text)
        if match:
            roots.add(Path(match.group(1)).resolve())
            continue
        path = Path(text)
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved.is_file() or resolved.suffix:
            roots.add(resolved.parent)
        else:
            roots.add(resolved if resolved.exists() else resolved.parent)
    if data_root and not roots:
        roots.add(Path(data_root).resolve())
    return sorted(roots)


def _docker_client():
    try:
        import docker
    except ImportError as error:
        raise RuntimeError(
            "TOOL_RUNTIME=docker requires the docker Python package"
        ) from error
    try:
        client = docker.from_env()
        client.ping()
    except Exception as error:  # noqa: BLE001 - surface any Engine connectivity failure
        raise RuntimeError("Docker Engine is unavailable: {}".format(error)) from error
    return client


class DockerExecutor:
    def __init__(self, client=None, registry: dict | None = None):
        self._client = client
        self._registry = registry

    @property
    def client(self):
        if self._client is None:
            self._client = _docker_client()
        return self._client

    @property
    def registry(self) -> dict:
        if self._registry is None:
            self._registry = load_registry()
        return self._registry

    def check_docker_available(self) -> None:
        self.client.ping()

    def ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except Exception as error:  # ImageNotFound and API errors
            raise RuntimeError("required Docker image is missing: {}".format(image)) from error

    def run_tool(
        self,
        command: Sequence[str],
        label: str,
        log_path: Path,
        timeout: float | None = None,
    ) -> None:
        tool = resolve_tool(command, self.registry)
        if tool is None:
            raise RuntimeError(
                "command is not a registered docker tool: {}".format(command[:1])
            )
        image = self.registry[tool]["image"]
        self.ensure_image(image)
        argv = docker_argv(command)
        roots = mount_roots_for(argv)
        volumes = {str(root): {"bind": str(root), "mode": "rw"} for root in roots}
        # Version probes may have no host paths; still bind the log parent for consistency.
        log_parent = str(log_path.resolve().parent)
        volumes.setdefault(log_parent, {"bind": log_parent, "mode": "rw"})
        workdir = str(roots[0]) if roots else log_parent
        labels = {TOOL_LABEL: tool}
        job_id = _job_id()
        if job_id:
            labels[JOB_LABEL] = job_id
        step_name = os.environ.get("_ASSEMBLY_STEP_NAME", "").strip()
        if step_name:
            labels["bacterial.assembly.step"] = step_name
        user = "{}:{}".format(os.getuid(), os.getgid())
        header = "\n== {} ==\nDOCKER IMAGE: {}\nCOMMAND: {}\n".format(
            label, image, subprocess.list2cmdline(argv)
        )
        print(header, end="", flush=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(header)
            log_file.flush()
            container = self.client.containers.run(
                image=image,
                command=argv,
                entrypoint=[],
                detach=True,
                volumes=volumes,
                working_dir=workdir,
                nano_cpus=_cpu_nano(),
                mem_limit=_memory_limit_bytes(),
                labels=labels,
                user=user,
                network_mode="none",
                privileged=False,
            )
            step_id = os.environ.get("_ASSEMBLY_STEP_ID", "").strip()
            if step_id:
                try:
                    from web.step_store import set_container

                    set_container(step_id, container.id, image)
                except Exception:
                    pass
            try:
                self._stream_logs(container, log_file, timeout=timeout, job_id=job_id)
                result = container.wait()
                status = int(result.get("StatusCode", 1))
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        if status:
            raise subprocess.CalledProcessError(status, argv)

    def _stream_logs(
        self, container, log_file, timeout: float | None, job_id: str | None = None
    ) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        last_cancel_check = 0.0
        try:
            for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                now = time.monotonic()
                if deadline is not None and now > deadline:
                    container.stop(timeout=10)
                    raise TimeoutError("container timed out for {}".format(container.id))
                if job_id and now - last_cancel_check >= 2.0:
                    last_cancel_check = now
                    if _job_cancel_requested(job_id):
                        container.stop(timeout=10)
                        raise RuntimeError("job cancelled")
                text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                print(text, end="", flush=True)
                log_file.write(text)
                log_file.flush()
        except TimeoutError:
            raise
        except RuntimeError:
            raise
        except Exception:
            # Fall back to waiting if log stream ends early.
            while container.status in {"created", "running"} or container.status is None:
                container.reload()
                if container.status not in {"created", "running"}:
                    break
                if deadline is not None and time.monotonic() > deadline:
                    container.stop(timeout=10)
                    raise TimeoutError("container timed out for {}".format(container.id))
                if job_id and _job_cancel_requested(job_id):
                    container.stop(timeout=10)
                    raise RuntimeError("job cancelled")
                time.sleep(0.2)

    def stop_job_containers(self, job_id: str, grace_seconds: float = 5.0) -> int:
        if not job_id:
            return 0
        stopped = 0
        filters = {"label": "{}={}".format(JOB_LABEL, job_id)}
        for container in self.client.containers.list(all=True, filters=filters):
            try:
                container.reload()
                if container.status in {"running", "created", "paused", "restarting"}:
                    container.stop(timeout=max(1, int(grace_seconds)))
                    stopped += 1
                container.remove(force=True)
            except Exception:
                continue
        return stopped

    def cleanup_orphans(self, active_job_ids: set[str] | None = None) -> int:
        """Stop labeled tool containers whose job is not in the active set."""
        active = active_job_ids or set()
        removed = 0
        for container in self.client.containers.list(
            all=True, filters={"label": TOOL_LABEL}
        ):
            labels = container.labels or {}
            job_id = labels.get(JOB_LABEL, "")
            if job_id and job_id in active:
                continue
            try:
                if container.status in {"running", "created", "paused", "restarting"}:
                    container.stop(timeout=5)
                container.remove(force=True)
                removed += 1
            except Exception:
                continue
        return removed


_DEFAULT_EXECUTOR: DockerExecutor | None = None


def get_executor() -> DockerExecutor:
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        _DEFAULT_EXECUTOR = DockerExecutor()
    return _DEFAULT_EXECUTOR


def reset_executor_for_tests() -> None:
    global _DEFAULT_EXECUTOR
    _DEFAULT_EXECUTOR = None
