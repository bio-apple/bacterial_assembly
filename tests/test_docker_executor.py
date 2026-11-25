import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.docker_executor import (
    DockerExecutor,
    mount_roots_for,
    reset_executor_for_tests,
    tool_runtime,
)
from scripts.software_registry import (
    docker_argv,
    load_registry,
    resolve_tool,
    clear_registry_cache,
)


class FakeContainer:
    def __init__(self, status_code=0, log_text=b"ok\n", status="running"):
        self.status_code = status_code
        self.log_text = log_text
        self.status = status
        self.id = "fake"
        self.labels = {}
        self.removed = False
        self.stopped = False

    def logs(self, stream=True, follow=True, stdout=True, stderr=True):
        yield self.log_text

    def wait(self):
        self.status = "exited"
        return {"StatusCode": self.status_code}

    def reload(self):
        return None

    def stop(self, timeout=5):
        self.stopped = True
        self.status = "exited"

    def remove(self, force=False):
        self.removed = True


class FakeContainers:
    def __init__(self, client):
        self.client = client
        self.items = []

    def run(self, **kwargs):
        self.client.last_run = kwargs
        container = FakeContainer(
            status_code=self.client.next_status,
            log_text=self.client.next_log,
        )
        container.labels = kwargs.get("labels") or {}
        self.items.append(container)
        return container

    def list(self, all=True, filters=None):
        return list(self.items)


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers(self)
        self.images = mock.Mock()
        self.images.get = mock.Mock(return_value=object())
        self.next_status = 0
        self.next_log = b"tool output\n"
        self.last_run = None
        self.ping = mock.Mock()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        clear_registry_cache()

    def test_registry_loads_pinned_images(self):
        tools = load_registry()
        self.assertEqual(tools["fastp"]["image"], "bacterial-assembly-fastp:0.23.4")
        self.assertEqual(resolve_tool(["fastp", "--version"]), "fastp")
        self.assertEqual(resolve_tool(["spades.py", "-o", "/tmp"]), "spades")
        self.assertEqual(
            resolve_tool(["micromamba", "run", "-n", "unicycler", "unicycler", "-h"]),
            "unicycler",
        )
        self.assertEqual(
            docker_argv(["micromamba", "run", "-n", "unicycler", "unicycler", "-1", "a"]),
            ["unicycler", "-1", "a"],
        )


class RuntimeTests(unittest.TestCase):
    def test_default_runtime_is_local(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOOL_RUNTIME", None)
            self.assertEqual(tool_runtime(), "local")

    def test_rejects_unknown_runtime(self):
        with mock.patch.dict(os.environ, {"TOOL_RUNTIME": "k8s"}):
            with self.assertRaises(RuntimeError):
                tool_runtime()


class MountTests(unittest.TestCase):
    def test_mounts_job_root_from_absolute_paths(self):
        job = "/data/jobs/11111111-1111-1111-1111-111111111111"
        roots = mount_roots_for(
            [
                "fastp",
                "--in1",
                "{}/input/r1.fq.gz".format(job),
                "--out1",
                "{}/work/fastp/out.fq.gz".format(job),
            ]
        )
        self.assertEqual(roots, [Path(job).resolve()])


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        reset_executor_for_tests()
        clear_registry_cache()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmpdir.name) / "run.log"
        self.client = FakeClient()
        self.executor = DockerExecutor(client=self.client)

    def tearDown(self):
        self.tmpdir.cleanup()
        reset_executor_for_tests()

    def test_runs_whitelisted_tool_with_limits_and_labels(self):
        job = "22222222-2222-2222-2222-222222222222"
        out = Path(self.tmpdir.name) / "jobs" / job / "work" / "fastp"
        out.mkdir(parents=True)
        command = [
            "fastp",
            "--in1",
            str(out / "in.fq"),
            "--out1",
            str(out / "out.fq"),
        ]
        with mock.patch.dict(
            os.environ,
            {
                "_ASSEMBLY_JOB_ID": job,
                "_ASSEMBLY_ASSIGNED_THREADS": "4",
                "_ASSEMBLY_METHOD_MEMORY_GIB": "2",
            },
        ):
            self.executor.run_tool(command, "fastp filtering", self.log_path)
        self.assertIsNotNone(self.client.last_run)
        self.assertEqual(self.client.last_run["image"], "bacterial-assembly-fastp:0.23.4")
        self.assertEqual(self.client.last_run["command"][0], "fastp")
        self.assertEqual(self.client.last_run["nano_cpus"], 4_000_000_000)
        self.assertEqual(self.client.last_run["mem_limit"], 3 * 1024 ** 3)
        self.assertEqual(self.client.last_run["labels"]["bacterial.assembly.job_id"], job)
        self.assertEqual(self.client.last_run["network_mode"], "none")
        self.assertFalse(self.client.last_run["privileged"])
        self.assertIn("DOCKER IMAGE:", self.log_path.read_text(encoding="utf-8"))
        self.assertTrue(self.client.containers.items[0].removed)

    def test_missing_image_raises(self):
        self.client.images.get.side_effect = Exception("missing")
        with self.assertRaises(RuntimeError) as ctx:
            self.executor.run_tool(["fastp", "--version"], "ver", self.log_path)
        self.assertIn("missing", str(ctx.exception))

    def test_nonzero_exit_raises(self):
        self.client.next_status = 7
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            self.executor.run_tool(["megahit", "--version"], "megahit", self.log_path)
        self.assertEqual(ctx.exception.returncode, 7)

    def test_stop_job_containers(self):
        container = FakeContainer(status="running")
        container.labels = {"bacterial.assembly.job_id": "abc"}
        self.client.containers.items = [container]
        stopped = self.executor.stop_job_containers("abc")
        self.assertEqual(stopped, 1)
        self.assertTrue(container.stopped)
        self.assertTrue(container.removed)


class CommonDockerBridgeTests(unittest.TestCase):
    def setUp(self):
        reset_executor_for_tests()
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()
        reset_executor_for_tests()
        os.environ.pop("TOOL_RUNTIME", None)

    def test_run_command_uses_local_by_default(self):
        from scripts import common

        log_path = Path(self.tmpdir.name) / "log.txt"
        with mock.patch.object(common.subprocess, "Popen") as popen:
            process = mock.Mock()
            process.stdout = iter(["hi\n"])
            process.wait.return_value = 0
            popen.return_value = process
            common.run_command(["echo", "hi"], "echo", log_path)
            popen.assert_called_once()

    def test_run_command_routes_to_docker(self):
        from scripts import common
        from scripts.docker_executor import get_executor

        log_path = Path(self.tmpdir.name) / "log.txt"
        client = FakeClient()
        reset_executor_for_tests()
        with mock.patch("scripts.docker_executor._DEFAULT_EXECUTOR", DockerExecutor(client=client)):
            with mock.patch.dict(os.environ, {"TOOL_RUNTIME": "docker"}):
                # Re-bind get_executor to use our patched default after reset.
                import scripts.docker_executor as de

                de._DEFAULT_EXECUTOR = DockerExecutor(client=client)
                common.run_command(
                    ["fastp", "--in1", str(Path(self.tmpdir.name) / "a.fq")],
                    "fastp",
                    log_path,
                )
        self.assertEqual(client.last_run["image"], "bacterial-assembly-fastp:0.23.4")


if __name__ == "__main__":
    unittest.main()
