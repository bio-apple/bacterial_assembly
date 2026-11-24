import shutil
import tempfile
import unittest
from pathlib import Path

from web.host_monitor import format_bytes, snapshot


class HostMonitorTests(unittest.TestCase):
    def test_snapshot_uses_the_real_data_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reading = snapshot(root)
            usage = shutil.disk_usage(root)
        self.assertTrue(reading["disk"]["ok"])
        self.assertEqual(reading["disk"]["total_bytes"], usage.total)
        self.assertEqual(reading["disk"]["used_bytes"], usage.used)
        self.assertGreaterEqual(reading["disk"]["percent"], 0)
        self.assertLessEqual(reading["disk"]["percent"], 100)
        self.assertIn(format_bytes(usage.total).split()[-1], ("B", "KiB", "MiB", "GiB", "TiB"))

    def test_cpu_and_memory_are_measured_or_reported_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            reading = snapshot(Path(directory))
        for name in ("cpu", "memory"):
            metric = reading[name]
            self.assertIn("ok", metric)
            if metric["ok"]:
                self.assertGreaterEqual(metric["percent"], 0)
                self.assertLessEqual(metric["percent"], 100)
            else:
                self.assertTrue(metric["error"])


if __name__ == "__main__":
    unittest.main()
