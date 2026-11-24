import unittest
from unittest.mock import patch

from scripts.common import cpu_share, memory_share_gib, physical_memory_bytes


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


if __name__ == "__main__":
    unittest.main()
