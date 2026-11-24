import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["ASSEMBLY_WEB_AUTOCREATE"] = "0"

from werkzeug.security import generate_password_hash

from scripts.common import cpu_share
from web.app import SCHEMA, create_app, elapsed_seconds, format_duration
from web.worker import process_next_job


class WebWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = self.root / "fake_pipeline.py"
        self.pipeline.write_text(
            "import pathlib, sys\n"
            "args = sys.argv\n"
            "if '--threads' in args or '--memory' in args: raise SystemExit('manual resource setting passed')\n"
            "out = pathlib.Path(args[args.index('--outdir') + 1])\n"
            "if any(out.iterdir()): raise SystemExit('output directory must be empty')\n"
            "(out / 'assembled.txt').write_text('complete')\n"
            "(out / 'assemblers.txt').write_text(','.join(args[args.index('--assembler'):][1::2]))\n"
            "print('mock analysis complete', flush=True)\n",
            encoding="utf-8",
        )
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key-not-for-production-32bytes",
                "SESSION_COOKIE_SECURE": False,
                "USERS": {
                    "alice": generate_password_hash("alice-password", method="scrypt"),
                    "bob": generate_password_hash("bob-password", method="scrypt"),
                },
                "DATA_ROOT": self.root / "data",
                "PIPELINE_PATH": self.pipeline,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client, username="alice", password="alice-password"):
        page = client.get("/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = client.post(
            "/login",
            data={"csrf_token": token, "username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/modules")
        dashboard = client.get("/")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', dashboard.data)
        self.assertIsNotNone(token)
        return token.group(1).decode()

    def submit_job(self, client, token, order=("spades", "megahit")):
        return client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler_order": json.dumps(list(order)),
                "assembler": list(order),
                "sample": "isolate_17",
                "r1": (io.BytesIO(b"@r1\nACGT\n+\nIIII\n"), "reads_R1.fastq.gz"),
                "r2": (io.BytesIO(b"@r2\nTGCA\n+\nIIII\n"), "reads_R2.fastq.gz"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )

    def test_auth_csrf_ordered_upload_isolation_and_download(self):
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)
        self.assertEqual(self.client.get("/history").status_code, 302)
        self.assertEqual(self.client.get("/modules").status_code, 302)
        login_page = self.client.get("/login")
        self.assertIn(b'id="toggle-password"', login_page.data)
        self.assertIn('aria-label="显示密码"'.encode(), login_page.data)
        token = self.login(self.client)
        module_page = self.client.get("/modules")
        self.assertIn("微生物 NGS".encode(), module_page.data)
        self.assertIn("可用分析模块".encode(), module_page.data)
        self.assertIn("细菌基因组组装".encode(), module_page.data)
        self.assertIn(b'href="/"', module_page.data)
        dashboard = self.client.get("/")
        self.assertIn(b'id="upload-progress"', dashboard.data)
        self.assertIn("R1 / R2 文件上传进度".encode(), dashboard.data)
        self.assertIn("所选组装方法按选择顺序串行执行".encode(), dashboard.data)
        self.assertNotIn(b'name="threads"', dashboard.data)
        self.assertNotIn(b'name="memory"', dashboard.data)
        invalid = self.client.post("/api/jobs", data={})
        self.assertEqual(invalid.status_code, 400)
        response = self.submit_job(self.client, token)
        self.assertEqual(response.status_code, 202)
        job_id = response.json["job"]["id"]

        queued = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["module_id"], "bacterial-assembly")
        self.assertEqual(queued["assemblers"], ["spades", "megahit"])
        self.assertEqual(queued["sample"], "isolate_17")
        self.assertEqual(queued["threads"], cpu_share())
        self.assertNotIn("memory", queued)
        self.assertEqual(self.client.get("/api/jobs/{}/log".format(job_id)).status_code, 200)

        history = self.client.get("/history")
        self.assertIn("isolate_17".encode(), history.data)
        self.assertIn("细菌基因组组装".encode(), history.data)
        self.assertIn("spades".encode(), history.data)
        self.assertIn("排队中".encode(), history.data)
        self.assertIn("等待开始".encode(), history.data)
        self.assertIn("下载运行日志".encode(), history.data)
        self.assertIn("完成后可下载".encode(), history.data)
        self.assertNotIn("下载输出结果".encode(), history.data)
        queued_log = self.client.get("/api/jobs/{}/log/download".format(job_id))
        self.assertEqual(queued_log.status_code, 200)
        self.assertIn(b"MODULE: bacterial-assembly", queued_log.data)
        self.assertIn(b"STATUS: queued", queued_log.data)
        self.assertIn("attachment", queued_log.headers["Content-Disposition"])
        queued_log.close()

        bob = self.app.test_client()
        bob_token = self.login(bob, "bob", "bob-password")
        self.assertEqual(bob.get("/api/jobs").json["jobs"], [])
        self.assertNotIn("isolate_17".encode(), bob.get("/history").data)
        self.assertEqual(bob.get("/api/jobs/{}".format(job_id)).status_code, 404)
        self.assertEqual(bob.get("/api/jobs/{}/download".format(job_id)).status_code, 404)
        self.assertEqual(
            bob.get("/api/jobs/{}/log/download".format(job_id)).status_code, 404
        )

        self.assertTrue(process_next_job(self.app))
        done = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(done["status"], "succeeded")
        history = self.client.get("/history")
        self.assertIn("已完成".encode(), history.data)
        self.assertIn("结束时间".encode(), history.data)
        self.assertIn("运行时间".encode(), history.data)
        self.assertRegex(history.data.decode(), r'class="runtime-value">\s*\d+秒')
        self.assertIn("下载输出结果".encode(), history.data)
        self.assertIn(
            '/api/jobs/{}/download'.format(job_id).encode(),
            history.data,
        )
        self.assertNotIn("等待开始".encode(), history.data)
        result_dir = Path(self.app.config["RESULT_ROOT"]) / job_id
        self.assertEqual(
            (result_dir / "outputs" / "assemblers.txt").read_text(encoding="utf-8"),
            "spades,megahit",
        )
        download = self.client.get("/api/jobs/{}/download".format(job_id))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        with tarfile.open(fileobj=io.BytesIO(download.data), mode="r:gz") as archive:
            self.assertIn("results/assembled.txt", archive.getnames())
            self.assertIn("results/assemblers.txt", archive.getnames())
        download.close()
        log = self.client.get("/api/jobs/{}/log".format(job_id)).json["text"]
        self.assertIn("mock analysis complete", log)
        downloaded_log = self.client.get("/api/jobs/{}/log/download".format(job_id))
        self.assertIn("MODULE: bacterial-assembly", downloaded_log.get_data(as_text=True))
        self.assertIn("mock analysis complete", downloaded_log.get_data(as_text=True))
        downloaded_log.close()

    def test_jobs_are_claimed_fifo_one_at_a_time(self):
        token = self.login(self.client)
        first = self.submit_job(self.client, token, ("megahit",)).json["job"]["id"]
        token = re.search(
            rb'name="csrf-token" content="([^"]+)"', self.client.get("/").data
        ).group(1).decode()
        second = self.submit_job(self.client, token, ("unicycler",)).json["job"]["id"]

        self.assertTrue(process_next_job(self.app))
        statuses = {
            row["id"]: row["status"]
            for row in self.client.get("/api/jobs").json["jobs"]
        }
        self.assertEqual(statuses[first], "succeeded")
        self.assertEqual(statuses[second], "queued")

    def test_rejects_missing_assembler_and_invalid_order(self):
        token = self.login(self.client)
        response = self.client.post(
            "/api/jobs",
            data={
                "assembler_order": "[]",
                "r1": (io.BytesIO(b"read"), "r1.fastq"),
                "r2": (io.BytesIO(b"read"), "r2.fastq"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

        response = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(response.status_code, 202)

        page = self.client.get("/")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        mismatch = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["megahit"],
                "assembler_order": '["spades"]',
                "r1": (io.BytesIO(b"read"), "r1.fastq"),
                "r2": (io.BytesIO(b"read"), "r2.fastq"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(mismatch.status_code, 400)

    def test_api_rejects_manual_resource_settings(self):
        token = self.login(self.client)
        response = self.client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "threads": "2",
                "memory": "8",
                "r1": (io.BytesIO(b"read"), "r1.fastq"),
                "r2": (io.BytesIO(b"read"), "r2.fastq"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("automatic", response.json["error"])

    def test_worker_archives_partial_results_when_a_method_fails(self):
        self.pipeline.write_text(
            "import pathlib, sys\n"
            "args = sys.argv\n"
            "out = pathlib.Path(args[args.index('--outdir') + 1])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'comparison.tsv').write_text('assembler\\tstatus\\nspades\\tok\\n')\n"
            "(out / 'partial.txt').write_text('successful method output')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        token = self.login(self.client)
        response = self.submit_job(self.client, token, ("spades",))
        job_id = response.json["job"]["id"]

        self.assertTrue(process_next_job(self.app))
        job = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["archive_available"])
        self.assertIn("partial results", job["error"])
        self.assertIn("下载部分结果".encode(), self.client.get("/history").data)
        download = self.client.get("/api/jobs/{}/download".format(job_id))
        self.assertEqual(download.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(download.data), mode="r:gz") as archive:
            self.assertIn("results/partial.txt", archive.getnames())
        download.close()

    def test_analysis_module_navigation_uses_registry(self):
        self.app.add_url_rule(
            "/future-module",
            endpoint="future_module",
            view_func=lambda: "future module",
        )
        self.app.config["ANALYSIS_MODULES"] = (
            *self.app.config["ANALYSIS_MODULES"],
            {
                "id": "future-module",
                "name": "后续分析模块",
                "code": "FM",
                "endpoint": "future_module",
            },
        )
        self.login(self.client)
        page = self.client.get("/")
        self.assertIn(b'aria-current="page"', page.data)
        self.assertIn("细菌基因组组装".encode(), page.data)
        self.assertIn("后续分析模块".encode(), page.data)
        module_page = self.client.get("/modules")
        self.assertIn("后续分析模块".encode(), module_page.data)

    def test_short_secret_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
            create_app(
                {
                    "SECRET_KEY": "too-short",
                    "USERS": {"alice": generate_password_hash("alice-password")},
                    "DATA_ROOT": self.root / "short-secret",
                }
            )

    def test_existing_database_migrates_module_id(self):
        data_root = self.root / "legacy-data"
        data_root.mkdir()
        database = data_root / "jobs.sqlite3"
        legacy_schema = SCHEMA.replace(
            "    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',\n", ""
        ).replace("    threads INTEGER NOT NULL,\n", "    threads INTEGER NOT NULL,\n    memory INTEGER NOT NULL,\n")
        self.assertNotEqual(legacy_schema, SCHEMA)
        with closing(sqlite3.connect(database)) as db:
            db.executescript(legacy_schema)
            db.execute(
                "INSERT INTO jobs (id, owner, status, assemblers, threads, memory, "
                "sample, spades_careful, input_r1, input_r2, output_dir, log_path) "
                "VALUES ('legacy-job', 'alice', 'queued', '[]', 1, 1, 'legacy', 0, "
                "'r1.fastq', 'r2.fastq', 'output', 'job.log')"
            )
            db.commit()
        create_app(
            {
                "SECRET_KEY": "test-key-not-for-production-32bytes",
                "USERS": {"alice": generate_password_hash("alice-password", method="scrypt")},
                "DATA_ROOT": data_root,
                "PIPELINE_PATH": self.pipeline,
            }
        )
        with closing(sqlite3.connect(database)) as db:
            module_id = db.execute(
                "SELECT module_id FROM jobs WHERE id = 'legacy-job'"
            ).fetchone()[0]
            row = db.execute(
                "SELECT sample, threads FROM jobs WHERE id = 'legacy-job'"
            ).fetchone()
            columns = {record[1] for record in db.execute("PRAGMA table_info(jobs)")}
        self.assertEqual(module_id, "bacterial-assembly")
        self.assertEqual(row, ("legacy", 1))
        self.assertNotIn("memory", columns)

    def test_history_runtime_formatting(self):
        self.assertEqual(
            elapsed_seconds("2026-01-01T00:00:00Z", "2026-01-01T01:01:05Z"),
            3665,
        )
        self.assertEqual(format_duration(3665), "1小时 1分")
        self.assertIsNone(elapsed_seconds(None, None))

    def test_upload_storage_error_returns_gateway_storage_status(self):
        token = self.login(self.client)
        with patch("web.app.Path.mkdir", autospec=True, side_effect=OSError("disk full")):
            response = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(response.status_code, 507)

    def test_configured_accounts_are_required(self):
        users_file = self.root / "users.json"
        users_file.write_text('{"users":{}}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            create_app(
                {
                    "SECRET_KEY": "test-key-not-for-production-32bytes",
                    "USERS_FILE": users_file,
                    "DATA_ROOT": self.root / "other",
                }
            )


if __name__ == "__main__":
    unittest.main()
