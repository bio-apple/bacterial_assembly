import unittest
from unittest import mock

from web.migrate_pg import split_sql
from web.step_store import is_infra_error


class MigrateSqlTests(unittest.TestCase):
    def test_split_sql_skips_comments_and_splits_statements(self):
        script = """
        -- header
        CREATE TABLE a (id INT);
        CREATE TABLE b (id INT);
        """
        parts = split_sql(script)
        self.assertEqual(len(parts), 2)
        self.assertIn("CREATE TABLE a", parts[0])
        self.assertIn("CREATE TABLE b", parts[1])


class InfraErrorTests(unittest.TestCase):
    def test_detects_docker_engine_errors(self):
        self.assertTrue(is_infra_error("Docker Engine is unavailable: timeout"))
        self.assertFalse(is_infra_error("spades exited with code 1"))


class QueueRoutingTests(unittest.TestCase):
    def test_enqueue_routes_qc_and_assembly(self):
        from web import queueing
        import web.rq_jobs as rq_jobs

        with mock.patch(
            "web.step_store.enqueue_ready",
            return_value={"attempt": 2, "step_name": "noop"},
        ):
            with mock.patch.object(queueing, "queue") as queue_factory:
                fake_q = mock.Mock()
                queue_factory.return_value = fake_q
                queueing.enqueue_step("step-1", "qc")
                queue_factory.assert_called_with("qc")
                args, kwargs = fake_q.enqueue.call_args
                self.assertIs(args[0], rq_jobs.run_qc_step)
                self.assertEqual(args[1], "step-1")
                self.assertEqual(kwargs["job_id"], "step-step-1-2")

                queue_factory.reset_mock()
                fake_q.reset_mock()
                queueing.enqueue_step("step-2", "assembly")
                queue_factory.assert_called_with("assembly")
                self.assertIs(
                    fake_q.enqueue.call_args.args[0], rq_jobs.run_assembly_step
                )


class NoopJobTests(unittest.TestCase):
    def test_qc_noop_claims_and_succeeds(self):
        from web import rq_jobs

        claimed = {
            "id": "s1",
            "job_id": "j1",
            "step_type": "noop",
            "worker_type": "qc",
            "status": "running",
        }
        with mock.patch.object(rq_jobs, "claim_step", return_value=claimed):
            with mock.patch.object(rq_jobs, "heartbeat_job"):
                with mock.patch.object(rq_jobs.step_runners, "load_job", return_value={"id": "j1"}):
                    with mock.patch.object(rq_jobs, "finish_step") as finish:
                        with mock.patch.object(rq_jobs, "on_step_finished") as nxt:
                            result = rq_jobs.run_qc_step("s1")
        self.assertEqual(result, "succeeded")
        finish.assert_called_once()
        self.assertEqual(finish.call_args.kwargs["status"], "succeeded")
        nxt.assert_called_once_with("s1", "succeeded")

    def test_claim_none_skips(self):
        from web import rq_jobs

        with mock.patch.object(rq_jobs, "claim_step", return_value=None):
            self.assertEqual(rq_jobs.run_assembly_step("s1"), "skipped")


if __name__ == "__main__":
    unittest.main()
