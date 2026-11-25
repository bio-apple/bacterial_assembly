import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.common import cpu_share, memory_share_gib, physical_memory_bytes
from scripts.resource_limits import (
    allocation_log,
    combine_cpu,
    combine_memory,
    parse_cpu_max,
    parse_memory_limit,
    read_cgroup,
)


class ResourceShareTests(unittest.TestCase):
    def test_cpu_share_uses_ninety_percent_rounded_down(self):
        for logical_cpus, expected in (
            (1, 1),
            (2, 1),
            (5, 4),
            (9, 8),
            (10, 9),
            (32, 28),
        ):
            with self.subTest(logical_cpus=logical_cpus):
                self.assertEqual(cpu_share(logical_cpus), expected)

    def test_cpu_share_uses_at_least_one_thread_when_cpu_count_is_unavailable(self):
        with patch("scripts.common.os.cpu_count", return_value=None):
            self.assertEqual(cpu_share(), 1)

    def test_memory_share_uses_ninety_percent_rounded_down_to_gib(self):
        gib = 1024 ** 3
        for total, expected in (
            (gib, 1),
            (2 * gib, 1),
            (10 * gib, 9),
            (16 * gib, 14),
            (64 * gib + 500, 57),
        ):
            with self.subTest(total=total):
                self.assertEqual(memory_share_gib(total), expected)

    def test_reads_physical_memory_on_this_machine(self):
        self.assertGreaterEqual(physical_memory_bytes(), 1024 ** 3)
        self.assertGreaterEqual(memory_share_gib(), 1)

    def test_cgroup_v2_uses_the_tighter_cpu_and_memory_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cgroup = root / "sys/fs/cgroup/machine.slice"
            cgroup.mkdir(parents=True)
            (cgroup / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
            (cgroup / "cpuset.cpus.effective").write_text("0-7\n", encoding="utf-8")
            (cgroup / "memory.max").write_text("2147483648\n", encoding="utf-8")
            found = read_cgroup("0::/machine.slice\n", root / "sys/fs/cgroup")
            cpu, source = combine_cpu(16, found["quota_cpus"], found["cpuset_cpus"])
            memory, memory_source = combine_memory(8 * 1024 ** 3, found["memory_bytes"])
        self.assertEqual(found["generation"], "cgroup-v2")
        self.assertEqual((cpu, source), (2, "cgroup"))
        self.assertEqual((memory, memory_source), (2147483648, "cgroup"))
        self.assertEqual(cpu_share(cpu), 1)
        self.assertIn("cgroup-v2", allocation_log(16, cpu, "cgroup-v2", memory, "cgroup-v2", 1, 1))

    def test_cgroup_v1_quota_and_cpuset_fall_back_when_unlimited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sys/fs/cgroup"
            cpu = root / "cpu,cpuacct/docker/abc"
            cpuset = root / "cpuset/docker/abc"
            memory = root / "memory/docker/abc"
            for path in (cpu, cpuset, memory):
                path.mkdir(parents=True)
            (cpu / "cpu.cfs_quota_us").write_text("-1\n", encoding="utf-8")
            (cpu / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
            (cpuset / "cpuset.cpus").write_text("0,2-3\n", encoding="utf-8")
            (memory / "memory.limit_in_bytes").write_text(str(1 << 62), encoding="utf-8")
            proc = "\n".join(
                (
                    "4:cpu,cpuacct:/docker/abc",
                    "3:cpuset:/docker/abc",
                    "2:memory:/docker/abc",
                )
            )
            found = read_cgroup(proc, root)
            limited, source = combine_cpu(8, found["quota_cpus"], found["cpuset_cpus"])
            total, memory_source = combine_memory(4 * 1024 ** 3, found["memory_bytes"])
        self.assertEqual(found["generation"], "cgroup-v1")
        self.assertIsNone(found["quota_cpus"])
        self.assertEqual((limited, source), (3, "cgroup"))
        self.assertEqual(memory_source, "host")
        self.assertEqual(total, 4 * 1024 ** 3)
        self.assertIsNone(parse_cpu_max("max 100000"))
        self.assertIsNone(parse_memory_limit("max"))

    def test_direct_script_import_can_read_the_resource_limit(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'scripts'); from common import cpu_share; print(cpu_share())",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(int(result.stdout.strip()), 1)

    def test_unreadable_cgroup_keeps_the_host_count(self):
        cpu, source = combine_cpu(5, None, None)
        self.assertEqual((cpu, source), (5, "host"))
        self.assertEqual(cpu_share(cpu), 4)


if __name__ == "__main__":
    unittest.main()
