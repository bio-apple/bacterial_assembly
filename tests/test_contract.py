import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ["ASSEMBLY_WEB_AUTOCREATE"] = "0"

from run_pipeline import build_parser
from web.app import ensure_partial_success_status
from web.fastq_check import check_read_pair, gzip_is_intact
from web.worker import build_pipeline_command, finished_status


ROOT = Path(__file__).resolve().parents[1]


class PipelineContractTests(unittest.TestCase):
    def test_cli_parser_accepts_the_worker_command_and_a_standalone_run(self):
        job_id = str(uuid.uuid4())
        command = build_pipeline_command(
            ROOT / "run_pipeline.py",
            job_id,
            "r1.fastq",
            "r2.fastq",
            "out",
            "isolate_17",
            ["spades", "megahit"],
            "21,33",
            True,
        )
        parsed = build_parser().parse_args(command[2:])
        self.assertEqual(parsed.job_id, job_id)
        self.assertEqual(parsed.assembler, ["spades", "megahit"])
        self.assertEqual(parsed.spades_kmers, [21, 33])
        self.assertTrue(parsed.spades_careful)
        bare = build_parser().parse_args(
            ["--r1", "a.fastq", "--r2", "b.fastq", "--outdir", "out", "--assembler", "megahit"]
        )
        self.assertIsNone(bare.job_id)

        missing = ROOT / "missing-r1.fastq"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_pipeline.py"),
                "--r1",
                str(missing),
                "--r2",
                str(missing),
                "--outdir",
                str(ROOT / "missing-out"),
                "--assembler",
                "spades",
                "--job-id",
                job_id,
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("unrecognized arguments", completed.stderr)
        self.assertIn("missing", completed.stderr.lower())

    def test_finished_status_follows_the_outcome_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            outdir = Path(temporary)
            (outdir / "assembly_outcome.json").write_text(
                json.dumps({"status": "partial_success"}),
                encoding="utf-8",
            )
            self.assertEqual(finished_status(3, outdir), "partial_success")
            (outdir / "assembly_outcome.json").write_text(
                json.dumps({"status": "failed"}),
                encoding="utf-8",
            )
            self.assertEqual(finished_status(1, outdir), "failed")
            (outdir / "assembly_outcome.json").unlink()
            self.assertEqual(finished_status(0, outdir), "succeeded")
            self.assertEqual(finished_status(1, outdir), "failed")

    def test_pair_check_rejects_count_and_id_mismatches_and_broken_gzip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "broken.fastq.gz"
            broken.write_bytes(b"not gzip")
            self.assertFalse(gzip_is_intact(broken))
            left = root / "R1.fastq"
            right = root / "R2.fastq"
            left.write_text("@pair/1\nAC\n+\nII\n@pair/1\nGG\n+\nII\n", encoding="utf-8")
            right.write_text("@pair/2\nAC\n+\nII\n", encoding="utf-8")
            with self.assertRaises(ValueError) as count_error:
                check_read_pair(left, right, root / "count")
            self.assertIn("数量不一致", str(count_error.exception))
            self.assertTrue((root / "count" / "fastq_pair.json").is_file())
            right.write_text("@other/2\nAC\n+\nII\n@other/2\nGG\n+\nII\n", encoding="utf-8")
            with self.assertRaises(ValueError) as id_error:
                check_read_pair(left, right, root / "ids")
            self.assertIn("read 编号不一致", str(id_error.exception))
            right.write_text("@pair/2\nAC\n+\nII\n@pair/2\nGG\n+\nII\n", encoding="utf-8")
            check_read_pair(left, right, root / "ok")
            saved = json.loads((root / "ok" / "fastq_pair.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["r1_records"], 2)
            gzip_path = root / "R1.fastq.gz"
            with gzip.GzipFile(gzip_path, "wb", mtime=0) as handle:
                handle.write(left.read_bytes())
            self.assertTrue(gzip_is_intact(gzip_path))

    def test_old_jobs_remain_when_partial_success_is_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = sqlite3.connect(Path(temporary) / "jobs.sqlite3")
            database.execute(
                "CREATE TABLE jobs ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                "id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')), "
                "assemblers TEXT NOT NULL, threads INTEGER NOT NULL, sample TEXT NOT NULL, "
                "spades_careful INTEGER NOT NULL, input_r1 TEXT NOT NULL, input_r2 TEXT NOT NULL, "
                "output_dir TEXT NOT NULL, log_path TEXT NOT NULL)"
            )
            database.execute(
                "INSERT INTO jobs (id, owner, status, assemblers, threads, sample, "
                "spades_careful, input_r1, input_r2, output_dir, log_path) "
                "VALUES ('old', 'alice', 'succeeded', '[\"spades\"]', 1, 'kept', 0, "
                "'r1', 'r2', 'out', 'log')"
            )
            database.commit()
            ensure_partial_success_status(database)
            kept = database.execute("SELECT sample, status FROM jobs WHERE id = 'old'").fetchone()
            self.assertEqual(tuple(kept), ("kept", "succeeded"))
            database.execute(
                "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, sample, "
                "spades_careful, input_r1, input_r2, output_dir, log_path) "
                "VALUES ('new', 'alice', 'bacterial-assembly', 'partial_success', '[\"spades\"]', "
                "1, 'part', 0, 'r1', 'r2', 'out', 'log')"
            )
            database.commit()
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                2,
            )
            database.close()


if __name__ == "__main__":
    unittest.main()
