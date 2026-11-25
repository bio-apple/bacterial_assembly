import json
import unittest
from unittest import mock


class OrchestratorGraphTests(unittest.TestCase):
    def test_fastp_success_enqueues_assemblies(self):
        from web import orchestrator

        steps = {
            "fastp": {"id": "f1", "step_name": "fastp", "step_type": "fastp"},
            "spades": {"id": "a1", "step_name": "spades", "step_type": "assembly"},
            "quast:spades": {"id": "q1", "step_name": "quast:spades", "step_type": "quast"},
            "summary": {"id": "s1", "step_name": "summary", "step_type": "summary"},
        }
        job = {"id": "j1", "assemblers": json.dumps(["spades"]), "status": "running"}

        with mock.patch.object(orchestrator, "connection") as conn_ctx:
            cur = mock.MagicMock()
            cur.fetchone.side_effect = [
                {"id": "f1", "job_id": "j1", "step_name": "fastp", "step_type": "fastp"},
                job,
            ]
            cur.fetchall.return_value = list(steps.values())
            conn = mock.MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            conn_ctx.return_value.__enter__.return_value = conn
            with mock.patch.object(orchestrator, "_steps_by_name", return_value=steps):
                with mock.patch.object(orchestrator, "enqueue_step") as enqueue:
                    orchestrator.on_step_finished("f1", "succeeded")
        enqueue.assert_called_once_with("a1", "assembly")

    def test_assembly_failure_skips_quast(self):
        from web import orchestrator

        steps = {
            "spades": {
                "id": "a1",
                "job_id": "j1",
                "step_name": "spades",
                "step_type": "assembly",
                "software": "spades",
            },
            "quast:spades": {"id": "q1", "step_name": "quast:spades"},
            "summary": {"id": "s1", "step_name": "summary", "status": "pending"},
        }
        job = {"id": "j1", "assemblers": json.dumps(["spades"]), "status": "running"}
        with mock.patch.object(orchestrator, "connection") as conn_ctx:
            cur = mock.MagicMock()
            cur.fetchone.side_effect = [steps["spades"], job]
            conn = mock.MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            conn_ctx.return_value.__enter__.return_value = conn
            with mock.patch.object(orchestrator, "_steps_by_name", return_value=steps):
                with mock.patch.object(orchestrator, "enqueue_step"):
                    with mock.patch.object(orchestrator, "_skip_step") as skip:
                        with mock.patch.object(
                            orchestrator, "_assemblies_terminal", return_value=True
                        ):
                            with mock.patch.object(
                                orchestrator, "_quasts_terminal", return_value=True
                            ):
                                with mock.patch.object(
                                    orchestrator, "_maybe_enqueue_summary"
                                ) as maybe:
                                    orchestrator.on_step_finished("a1", "failed")
        skip.assert_called_once()
        maybe.assert_called_once_with("j1")


class TaskBackendTests(unittest.TestCase):
    def test_default_is_sqlite(self):
        from web.task_backend import task_backend

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("TASK_BACKEND", None)
            self.assertEqual(task_backend(), "sqlite")


if __name__ == "__main__":
    unittest.main()
