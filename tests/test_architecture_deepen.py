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
                        with mock.patch.object(orchestrator, "sync_sqlite_steps_from_pg"):
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


class JobSubmitTests(unittest.TestCase):
    def test_enqueue_skipped_without_rq(self):
        from web import job_submit

        with mock.patch.object(job_submit, "use_rq_pipeline", return_value=False):
            with mock.patch.object(job_submit, "plan_and_insert_job") as plan:
                used = job_submit.enqueue_rq_pipeline(
                    job_id="j1",
                    owner="u",
                    sample="s",
                    assemblers=["spades"],
                    threads=2,
                    spades_kmers=None,
                    spades_careful=False,
                    input_r1="//r1",
                    input_r2="//r2",
                    output_dir="/out",
                    log_path="/log",
                )
        self.assertFalse(used)
        plan.assert_not_called()

    def test_enqueue_plans_and_starts(self):
        from web import job_submit

        with mock.patch.object(job_submit, "use_rq_pipeline", return_value=True):
            with mock.patch.object(job_submit, "plan_and_insert_job") as plan:
                with mock.patch.object(job_submit, "start_job") as start:
                    used = job_submit.enqueue_rq_pipeline(
                        job_id="j1",
                        owner="u",
                        sample="s",
                        assemblers=["spades"],
                        threads=2,
                        spades_kmers=None,
                        spades_careful=False,
                        input_r1="//r1",
                        input_r2="//r2",
                        output_dir="/out",
                        log_path="/log",
                    )
        self.assertTrue(used)
        plan.assert_called_once()
        start.assert_called_once_with("j1")

    def test_submit_compensates_on_failure(self):
        from web import job_submit

        with mock.patch.object(job_submit, "enqueue_rq_pipeline", side_effect=RuntimeError("boom")):
            with mock.patch.object(job_submit, "compensate_failed_enqueue") as comp:
                with self.assertRaises(RuntimeError):
                    job_submit.submit_analysis_task(
                        job_id="j1",
                        owner="u",
                        sample="s",
                        assemblers=["spades"],
                        threads=2,
                        spades_kmers=None,
                        spades_careful=False,
                        input_r1="//r1",
                        input_r2="//r2",
                        output_dir="/out",
                        log_path="/log",
                    )
        comp.assert_called_once_with("j1")


class CancelPollTests(unittest.TestCase):
    def test_cancel_poll_injection(self):
        from scripts import docker_executor

        docker_executor.reset_executor_for_tests()
        self.assertFalse(docker_executor._job_cancel_requested("j1"))
        docker_executor.set_cancel_poll(lambda job_id: job_id == "j1")
        self.assertTrue(docker_executor._job_cancel_requested("j1"))
        docker_executor.reset_executor_for_tests()


class CancelRequestTests(unittest.TestCase):
    def test_request_cancel_stops_then_terminal(self):
        from web import cancel

        order = []

        class Cur:
            def execute(self, *args, **kwargs):
                order.append("sql")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Conn:
            def cursor(self):
                return Cur()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(cancel, "use_rq_pipeline", return_value=True):
            with mock.patch.object(cancel, "connection", return_value=Conn()):
                with mock.patch.object(cancel, "utc_now", return_value="t"):
                    with mock.patch.object(cancel, "_stop_containers", side_effect=lambda j: order.append("stop")):
                        with mock.patch.object(cancel, "mirror_job_status", side_effect=lambda *a, **k: order.append("mirror")):
                            with mock.patch.object(cancel, "sync_sqlite_steps_from_pg", side_effect=lambda j: order.append("sync")):
                                cancel.request_cancel("j1")
        self.assertIn("stop", order)
        self.assertLess(order.index("stop"), order.index("mirror"))


class RegistryBundledLabelTests(unittest.TestCase):
    def test_unicycler_bundled_label(self):
        from scripts.software_registry import clear_registry_cache, load_registry

        clear_registry_cache()
        entry = load_registry()["unicycler"]
        self.assertIn("bundled_spades_label", entry)
        self.assertIn("unicycler", entry["bundled_spades_label"])


class JobPublishTests(unittest.TestCase):
    def test_archive_results_dir(self):
        from web.job_publish import archive_results_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "results"
            results.mkdir()
            (results / "comparison.tsv").write_text("a\n", encoding="utf-8")
            archive = archive_results_dir("job-1", results, root)
            self.assertIsNotNone(archive)
            self.assertTrue(archive.is_file())
            self.assertGreater(archive.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
