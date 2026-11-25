import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class JobPathsTests(unittest.TestCase):
    def test_work_dir_prefers_sibling_work(self):
        from web.job_paths import work_dir_for_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "jobs" / "x" / "output"
            work = root / "jobs" / "x" / "work"
            work.mkdir(parents=True)
            output.mkdir(parents=True)
            self.assertEqual(work_dir_for_job({"output_dir": str(output)}), work)


class PipelineCoreTests(unittest.TestCase):
    def test_contig_map_matches_outcome(self):
        from scripts.assembly_outcome import ASSEMBLER_OUTPUTS
        from scripts.pipeline_core import ASSEMBLY_CONTIGS, ASSEMBLERS

        self.assertEqual(ASSEMBLY_CONTIGS, ASSEMBLER_OUTPUTS)
        self.assertEqual(set(ASSEMBLERS), set(ASSEMBLER_OUTPUTS))


class FinalizeStatusTests(unittest.TestCase):
    def test_finalize_uses_finished_status(self):
        from scripts.assembly_outcome import write_outcome
        from web import orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            write_outcome(
                work,
                "partial_success",
                ["spades", "megahit"],
                ["spades"],
                ["megahit"],
                3,
            )
            (work / "assemblies" / "spades").mkdir(parents=True)
            (work / "assemblies" / "spades" / "contigs.fasta").write_text(">a\nAT\n")
            job = {
                "id": "j1",
                "assemblers": json.dumps(["spades", "megahit"]),
                "output_dir": str(Path(tmp) / "output"),
            }
            assemblies = [
                {"step_name": "spades", "status": "succeeded"},
                {"step_name": "megahit", "status": "failed"},
            ]
            with mock.patch.object(orchestrator, "connection") as conn_ctx:
                cur = mock.MagicMock()
                cur.fetchone.return_value = job
                cur.fetchall.return_value = assemblies
                conn = mock.MagicMock()
                conn.cursor.return_value.__enter__.return_value = cur
                conn_ctx.return_value.__enter__.return_value = conn
                with mock.patch.object(orchestrator, "work_dir_for_job", return_value=work):
                    with mock.patch.object(orchestrator, "mirror_job_status") as mirror:
                        with mock.patch.object(orchestrator, "sync_sqlite_steps_from_disk"):
                            with mock.patch.object(orchestrator, "_publish_and_archive"):
                                orchestrator._finalize_job("j1")
            mirror.assert_called_with("j1", "partial_success", None)


class StepReadModelTests(unittest.TestCase):
    def test_maps_summary_to_comparison(self):
        from web import step_read_model

        pg_rows = [
            {
                "step_name": "fastp",
                "step_type": "fastp",
                "status": "succeeded",
                "started_at": None,
                "completed_at": None,
                "exit_code": 0,
                "error": None,
            },
            {
                "step_name": "summary",
                "step_type": "summary",
                "status": "succeeded",
                "started_at": None,
                "completed_at": None,
                "exit_code": 0,
                "error": None,
            },
        ]
        with mock.patch.object(step_read_model, "connection") as conn_ctx:
            cur = mock.MagicMock()
            cur.fetchall.return_value = pg_rows
            conn = mock.MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            conn_ctx.return_value.__enter__.return_value = conn
            rows = step_read_model.recorded_steps_from_pg("j1", ["spades"])
        names = {row["name"] for row in rows}
        self.assertIn("fastp", names)
        self.assertIn("comparison", names)


class ResourceEnvTests(unittest.TestCase):
    def test_apply_resource_env_sets_memory(self):
        from web import rq_jobs

        job = {"id": "j1", "threads": 4}
        with mock.patch.object(
            rq_jobs,
            "read_runtime_limits",
            return_value={"method_memory_gib": 7, "method_threads": 4},
        ):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("_ASSEMBLY_METHOD_MEMORY_GIB", None)
                rq_jobs._apply_resource_env(job)
                self.assertEqual(os.environ.get("_ASSEMBLY_METHOD_MEMORY_GIB"), "7")
                self.assertEqual(job["_memory_gib"], 7)


if __name__ == "__main__":
    unittest.main()
