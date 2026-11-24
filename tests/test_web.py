import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["ASSEMBLY_WEB_AUTOCREATE"] = "0"

_PROCESS_TEMP = tempfile.gettempdir()

from werkzeug.security import generate_password_hash

import run_pipeline
from scripts.common import cpu_share
from scripts.stage_record import StageRecord
from web.app import SCHEMA, create_app, elapsed_seconds, format_duration
from web.worker import process_next_job, stop_process_group


class WebWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_PROCESS_TEMP)
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
        self.assertEqual(response.headers["Location"], "/overview")
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
        self.assertEqual(self.client.get("/overview").status_code, 302)
        login_page = self.client.get("/login")
        self.assertIn(b'id="toggle-password"', login_page.data)
        self.assertIn('aria-label="显示密码"'.encode(), login_page.data)
        token = self.login(self.client)
        overview = self.client.get("/overview")
        self.assertIn("最近分析".encode(), overview.data)
        self.assertIn("先看这台机器".encode(), overview.data)
        self.assertIn("数据盘".encode(), overview.data)
        self.assertIn("细菌基因组组装".encode(), overview.data)
        self.assertNotIn(b"E.coli_001", overview.data)
        self.assertNotIn(b"Genome QC", overview.data)
        host = self.client.get("/api/host").json["host"]
        self.assertTrue(host["disk"]["ok"])
        self.assertIn(host["disk"]["total"].encode(), overview.data)
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
        data_root = Path(self.app.config["DATA_ROOT"])
        self.assertTrue((data_root / "jobs" / job_id / "input" / "R1.fastq.gz").is_file())
        self.assertTrue((data_root / "jobs" / job_id / "input" / "R2.fastq.gz").is_file())
        self.assertFalse((data_root / "uploads" / job_id).exists())
        self.assertFalse((data_root / "uploads" / "incoming" / job_id).exists())
        self.assertFalse((data_root / "uploads" / "validated" / job_id).exists())
        self.assertEqual(self.app.config["STAGING_ROOT"], data_root / "uploads" / "chunks")
        self.assertTrue((data_root / "reports").is_dir())
        self.assertEqual(self.app.config["DATABASE"], data_root / "database" / "jobs.sqlite3")

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
        self.assertIn("isolate_17".encode(), self.client.get("/overview").data)
        self.assertNotIn("isolate_17".encode(), bob.get("/overview").data)
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
        self.assertEqual(
            (data_root / "jobs" / job_id / "output" / "assemblers.txt").read_text(encoding="utf-8"),
            "spades,megahit",
        )
        self.assertTrue((data_root / "archive" / "{}.tar.gz".format(job_id)).is_file())
        self.assertTrue((data_root / "jobs" / job_id / "logs" / "job.log").is_file())
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
        legacy_database = data_root / "jobs.sqlite3"
        legacy_schema = SCHEMA.replace(
            "    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',\n", ""
        ).replace("    threads INTEGER NOT NULL,\n", "    threads INTEGER NOT NULL,\n    memory INTEGER NOT NULL,\n")
        self.assertNotEqual(legacy_schema, SCHEMA)
        with closing(sqlite3.connect(legacy_database)) as db:
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
        database = data_root / "database" / "jobs.sqlite3"
        self.assertFalse(legacy_database.exists())
        self.assertTrue(database.is_file())
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

    def test_job_monitor_keeps_status_and_selected_stages_apart(self):
        token = self.login(self.client)
        response = self.submit_job(self.client, token, ("shovill", "megahit"))
        self.assertEqual(response.status_code, 202)
        job_id = response.json["job"]["id"]

        queued = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["progress"]["status"], "queued")
        self.assertIsNone(queued["progress"]["current"]["id"])
        self.assertIsNone(queued["progress"]["current"]["software"])
        self.assertIsNone(queued["progress"]["current"]["percent"])
        self.assertIsNone(queued["progress"]["elapsed_seconds"])
        self.assertNotIn("overall_percent", queued)
        self.assertNotIn("overall_percent", queued["progress"])
        queued_ids = [stage["id"] for stage in queued["progress"]["stages"]]
        self.assertEqual(
            queued_ids,
            [
                "fastp",
                "shovill",
                "shovill:quast:original",
                "shovill:quast:gt500",
                "shovill:quast:gt1000",
                "megahit",
                "megahit:quast:original",
                "megahit:quast:gt500",
                "megahit:quast:gt1000",
                "comparison",
            ],
        )
        self.assertNotIn("spades", queued_ids)
        self.assertNotIn("unicycler", queued_ids)
        self.assertTrue(all(stage["state"] == "waiting" for stage in queued["progress"]["stages"]))
        self.assertTrue(all(stage["percent"] is None for stage in queued["progress"]["stages"]))

        stage_path = (
            Path(self.app.config["DATA_ROOT"]) / "jobs" / job_id / "work" / "stages.json"
        )
        record = StageRecord(stage_path, ["shovill", "megahit"])
        record.finish("fastp")
        record.finish("shovill")
        for evaluation in ("original", "gt500", "gt1000"):
            record.finish("shovill:quast:{}".format(evaluation))
        record.start("megahit")
        started = "2026-01-01T00:00:00Z"
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as database:
            database.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
                (started, started, job_id),
            )
            database.commit()

        running = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"]["status"], "running")
        self.assertEqual(running["progress"]["current"]["id"], "megahit")
        self.assertEqual(running["progress"]["current"]["software"], "MEGAHIT")
        self.assertEqual(running["progress"]["current"]["state"], "running")
        self.assertIsNone(running["progress"]["current"]["percent"])
        megahit = next(
            stage for stage in running["progress"]["stages"] if stage["id"] == "megahit"
        )
        self.assertEqual(megahit["state"], "running")
        self.assertIsNone(megahit["percent"])
        self.assertGreater(running["progress"]["elapsed_seconds"], 0)
        running_ids = [stage["id"] for stage in running["progress"]["stages"]]
        self.assertNotIn("spades", running_ids)
        self.assertNotIn("unicycler", running_ids)

        page = self.client.get("/jobs/{}".format(job_id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("当前软件".encode(), page.data)
        self.assertIn(b"MEGAHIT", page.data)
        self.assertIn("分析中".encode(), page.data)
        self.assertIn("未报告".encode(), page.data)
        self.assertIn("任务状态".encode(), page.data)
        self.assertIn("当前阶段".encode(), page.data)
        self.assertIn(b'data-stage-id="shovill"', page.data)
        self.assertIn(b'data-stage-id="megahit"', page.data)
        self.assertIn(b'data-stage-id="comparison"', page.data)
        self.assertNotIn(b'data-stage-id="spades"', page.data)
        self.assertNotIn(b'data-stage-id="unicycler"', page.data)
        self.assertNotIn(b"<progress", page.data)
        self.assertIn(b'id="cancel-job"', page.data)
        self.assertIn("取消任务".encode(), page.data)
        self.assertIn(b'id="job-log"', page.data)
        self.assertIn("/jobs/{}".format(job_id).encode(), self.client.get("/history").data)
        self.assertIn("查看进度".encode(), self.client.get("/history").data)
        self.assertIn(b'id="monitor-link"', self.client.get("/").data)

        bob = self.app.test_client()
        self.login(bob, "bob", "bob-password")
        self.assertEqual(bob.get("/api/jobs/{}".format(job_id)).status_code, 404)
        self.assertEqual(bob.get("/jobs/{}".format(job_id)).status_code, 404)

    def test_pipeline_stage_record_follows_selected_assemblers(self):
        outdir = self.root / "pipeline-stages"
        r1 = self.root / "stage_R1.fastq"
        r2 = self.root / "stage_R2.fastq"
        r1.write_text("@r\nAC\n+\nII\n", encoding="utf-8")
        r2.write_text("@r\nTG\n+\nII\n", encoding="utf-8")
        argv = [
            "run_pipeline.py",
            "--r1",
            str(r1),
            "--r2",
            str(r2),
            "--outdir",
            str(outdir),
            "--sample",
            "stage_sample",
            "--assembler",
            "shovill",
            "--assembler",
            "megahit",
        ]

        def fake_run_script(script, arguments, *args, **kwargs):
            if script == "run_megahit.py":
                raise subprocess.CalledProcessError(1, [script])

        with patch("run_pipeline.run_script", side_effect=fake_run_script), patch(
            "run_pipeline.run_contig_filter"
        ), patch("run_pipeline.run_summary"), patch(
            "run_pipeline.memory_share_gib", return_value=1
        ), patch(
            "run_pipeline.logical_cpu_count", return_value=2
        ), patch("sys.argv", argv):
            self.assertEqual(run_pipeline.main(), 1)

        record = json.loads((outdir / "stages.json").read_text(encoding="utf-8"))
        by_id = {stage["id"]: stage for stage in record["stages"]}
        self.assertEqual(
            list(by_id),
            [
                "fastp",
                "shovill",
                "shovill:quast:original",
                "shovill:quast:gt500",
                "shovill:quast:gt1000",
                "megahit",
                "megahit:quast:original",
                "megahit:quast:gt500",
                "megahit:quast:gt1000",
                "comparison",
            ],
        )
        self.assertNotIn("spades", by_id)
        self.assertNotIn("unicycler", by_id)
        self.assertEqual(by_id["fastp"]["state"], "done")
        self.assertEqual(by_id["shovill"]["state"], "done")
        self.assertEqual(by_id["megahit"]["state"], "failed")
        self.assertEqual(by_id["megahit"]["software"], "MEGAHIT")
        self.assertIsNone(by_id["megahit"]["percent"])
        self.assertEqual(by_id["megahit:quast:original"]["state"], "waiting")
        self.assertTrue(all(stage["percent"] is None for stage in record["stages"]))
        self.assertEqual(by_id["comparison"]["state"], "done")

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

    def use_small_chunks(self):
        self.app.config["UPLOAD_CHUNK_SIZE"] = 8

    def post_upload(self, client, token, filename, role, size):
        return client.post(
            "/api/uploads",
            json={"filename": filename, "size": size, "role": role},
            headers={"X-CSRF-Token": token},
        )

    def put_chunk(self, client, token, upload_id, index, payload):
        return client.put(
            "/api/uploads/{}/chunks/{}".format(upload_id, index),
            data=payload,
            headers={"X-CSRF-Token": token},
            content_type="application/octet-stream",
        )

    def store_chunks(self, client, token, filename, role, payload):
        self.use_small_chunks()
        created = self.post_upload(client, token, filename, role, len(payload))
        self.assertEqual(created.status_code, 201, created.get_json())
        upload = created.json["upload"]
        chunk_size = upload["chunk_size"]
        for index in range(upload["chunk_count"]):
            piece = payload[index * chunk_size:(index + 1) * chunk_size]
            stored = self.put_chunk(client, token, upload["id"], index, piece)
            self.assertEqual(stored.status_code, 200, stored.get_json())
        return upload

    def finish_upload(self, client, token, upload_id, digest):
        return client.post(
            "/api/uploads/{}/complete".format(upload_id),
            json={"sha256": digest},
            headers={"X-CSRF-Token": token},
        )

    def test_chunk_assemble_rejects_sha256_mismatch(self):
        token = self.login(self.client)
        payload = b"@r1\nACGTACGT\n+\nIIIIIIII\n"
        upload = self.store_chunks(self.client, token, "reads_R1.fastq", "r1", payload)
        rejected = self.finish_upload(self.client, token, upload["id"], "0" * 64)
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("SHA-256", rejected.json["error"])
        session_dir = self.app.config["STAGING_ROOT"] / upload["id"]
        validated = self.app.config["UPLOAD_VALIDATED"] / upload["id"] / "read.fastq"
        self.assertFalse(list(session_dir.glob("read.*")))
        self.assertFalse(validated.exists())
        self.assertTrue((session_dir / "chunks" / "00000000").is_file())
        status = self.client.get("/api/uploads/{}".format(upload["id"]))
        self.assertEqual(status.json["upload"]["status"], "failed")
        self.assertIsNone(status.json["upload"]["sha256"])
        self.assertFalse(any(self.app.config["JOBS_ROOT"].rglob("*.fastq")))
        accepted = self.finish_upload(
            self.client, token, upload["id"], hashlib.sha256(payload).hexdigest()
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        self.assertEqual(accepted.json["upload"]["status"], "completed")
        self.assertEqual(accepted.json["upload"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(validated.read_bytes(), payload)
        self.assertFalse(session_dir.exists())

    def test_resume_skips_stored_chunks(self):
        token = self.login(self.client)
        self.use_small_chunks()
        payload = b"ABCDEFGH1234"
        created = self.post_upload(self.client, token, "reads_R1.fq.gz", "r1", len(payload))
        upload_id = created.json["upload"]["id"]
        first = self.put_chunk(self.client, token, upload_id, 0, payload[:8])
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json["skipped"])
        chunk_path = self.app.config["STAGING_ROOT"] / upload_id / "chunks" / "00000000"
        modified = chunk_path.stat().st_mtime_ns
        second = self.put_chunk(self.client, token, upload_id, 0, payload[:8])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json["skipped"])
        self.assertEqual(chunk_path.stat().st_mtime_ns, modified)
        self.assertEqual(chunk_path.read_bytes(), payload[:8])
        self.assertEqual(second.json["upload"]["received"], [0])
        tail = self.put_chunk(self.client, token, upload_id, 1, payload[8:])
        self.assertEqual(tail.status_code, 200)
        self.assertFalse(tail.json["skipped"])
        done = self.finish_upload(
            self.client, token, upload_id, hashlib.sha256(payload).hexdigest()
        )
        self.assertEqual(done.status_code, 200, done.get_json())
        self.assertEqual(
            (self.app.config["UPLOAD_VALIDATED"] / upload_id / "read.fq.gz").read_bytes(),
            payload,
        )
        self.assertFalse((self.app.config["STAGING_ROOT"] / upload_id).exists())

    def test_upload_rejects_path_traversal(self):
        token = self.login(self.client)
        outside = self.root / "escaped.fastq"
        rejected = self.client.post(
            "/api/uploads",
            json={
                "filename": "../escaped.fastq",
                "size": 4,
                "role": "r1",
                "path": str(outside),
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(outside.exists())
        self.assertFalse(list(self.app.config["DATA_ROOT"].rglob("escaped.fastq")))
        missing = self.put_chunk(self.client, token, "not-a-uuid", 0, b"abcd")
        self.assertEqual(missing.status_code, 404)
        created = self.post_upload(self.client, token, "reads_R1.fastq", "r1", 4)
        upload_id = created.json["upload"]["id"]
        escaped = self.client.put(
            "/api/uploads/{}/chunks/0?path=/etc/passwd".format(upload_id),
            data=b"abcd",
            headers={"X-CSRF-Token": token},
            content_type="application/octet-stream",
        )
        self.assertEqual(escaped.status_code, 400)
        self.assertFalse((self.app.config["STAGING_ROOT"] / upload_id / "chunks" / "00000000").exists())
        paired = self.store_chunks(self.client, token, "reads_R2.fastq", "r2", b"AAAABBBBCCCC")
        digest = hashlib.sha256(b"AAAABBBBCCCC").hexdigest()
        self.assertEqual(self.finish_upload(self.client, token, paired["id"], digest).status_code, 200)
        job = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "sample": "isolate_17",
                "r1_session": paired["id"],
                "r2_session": paired["id"],
                "input_r1": "/etc/passwd",
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(job.status_code, 400)
        self.assertFalse(any(self.app.config["JOBS_ROOT"].rglob("passwd")))

    def test_incomplete_upload_cannot_create_job(self):
        token = self.login(self.client)
        self.use_small_chunks()
        pending = self.post_upload(self.client, token, "reads_R1.fastq", "r1", 12)
        partial = self.store_chunks(self.client, token, "reads_R2.fastq.gz", "r2", b"AAAABBBBCCCC")
        self.put_chunk(self.client, token, pending.json["upload"]["id"], 0, b"AAAAAAAA")
        response = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["megahit"],
                "assembler_order": '["megahit"]',
                "sample": "isolate_17",
                "r1_session": pending.json["upload"]["id"],
                "r2_session": partial["id"],
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能开始分析", response.json["error"])
        self.assertFalse(any(self.app.config["JOBS_ROOT"].rglob("*.fastq*")))
        self.assertTrue(
            (self.app.config["STAGING_ROOT"] / pending.json["upload"]["id"] / "chunks" / "00000000").is_file()
        )
        bob = self.app.test_client()
        bob_token = self.login(bob, "bob", "bob-password")
        self.assertEqual(
            bob.get("/api/uploads/{}".format(pending.json["upload"]["id"])).status_code, 404
        )
        stolen = bob.post(
            "/api/jobs",
            data={
                "assembler": ["megahit"],
                "assembler_order": '["megahit"]',
                "r1_session": pending.json["upload"]["id"],
                "r2_session": partial["id"],
            },
            headers={"X-CSRF-Token": bob_token},
        )
        self.assertEqual(stolen.status_code, 404)

    def test_completed_upload_can_create_job(self):
        token = self.login(self.client)
        page = self.client.get("/")
        self.assertIn("等待上传".encode(), page.data)
        self.assertIn("继续上传".encode(), page.data)
        self.assertNotIn("可以开始分析".encode(), page.data)
        left = b"@r1\nACGTAAAA\n+\nIIIIIIII\n"
        right = b"@r2\nTGCAAAAA\n+\nIIIIIIII\n"
        r1 = self.store_chunks(self.client, token, "reads_R1.fastq.gz", "r1", left)
        r2 = self.store_chunks(self.client, token, "sample.fq", "r2", right)
        self.assertEqual(
            self.finish_upload(self.client, token, r1["id"], hashlib.sha256(left).hexdigest()).status_code,
            200,
        )
        self.assertEqual(
            self.finish_upload(self.client, token, r2["id"], hashlib.sha256(right).hexdigest()).status_code,
            200,
        )
        response = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "sample": "isolate_17",
                "r1_session": r1["id"],
                "r2_session": r2["id"],
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        job_id = response.json["job"]["id"]
        input_dir = self.app.config["JOBS_ROOT"] / job_id / "input"
        self.assertEqual((input_dir / "R1.fastq.gz").read_bytes(), left)
        self.assertEqual((input_dir / "R2.fq").read_bytes(), right)
        self.assertFalse((self.app.config["STAGING_ROOT"] / r1["id"]).exists())
        self.assertFalse((self.app.config["STAGING_ROOT"] / r2["id"]).exists())
        self.assertFalse((self.app.config["JOBS_ROOT"] / job_id / "output" / "R1.fastq.gz").exists())
        again = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "sample": "isolate_18",
                "r1_session": r1["id"],
                "r2_session": r2["id"],
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertIn(again.status_code, (400, 404))

    def test_owner_can_cancel_queued_job_without_starting_it(self):
        token = self.login(self.client)
        response = self.submit_job(self.client, token, ("spades",))
        job_id = response.json["job"]["id"]
        job_dir = self.app.config["JOBS_ROOT"] / job_id
        log_path = job_dir / "logs" / "job.log"
        self.assertEqual(self.client.post("/api/jobs/{}/cancel".format(job_id)).status_code, 400)
        self.assertEqual(self.client.get("/api/jobs/{}".format(job_id)).json["job"]["status"], "queued")
        page = self.client.get("/jobs/{}".format(job_id))
        self.assertIn("取消任务".encode(), page.data)
        self.assertIn(b'id="cancel-job"', page.data)

        bob = self.app.test_client()
        bob_token = self.login(bob, "bob", "bob-password")
        hidden = bob.post(
            "/api/jobs/{}/cancel".format(job_id),
            headers={"X-CSRF-Token": bob_token},
        )
        self.assertEqual(hidden.status_code, 404)

        cancelled = self.client.post(
            "/api/jobs/{}/cancel".format(job_id),
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json["job"]["status"], "cancelled")
        self.assertFalse(process_next_job(self.app))
        self.assertEqual(
            self.client.get("/api/jobs/{}".format(job_id)).json["job"]["status"],
            "cancelled",
        )
        self.assertTrue(log_path.is_file())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("STATUS: queued", log_text)
        self.assertIn("STATUS: cancelled", log_text)
        self.assertTrue(job_dir.is_dir())
        self.assertTrue((job_dir / "output").is_dir())
        self.assertFalse((self.app.config["ARCHIVE_ROOT"] / "{}.tar.gz".format(job_id)).exists())
        detail = self.client.get("/jobs/{}".format(job_id))
        self.assertIn("已取消".encode(), detail.data)
        self.assertIn("运行日志已保留，结果不完整。".encode(), detail.data)
        self.assertIn(b'id="cancel-job" hidden', detail.data.replace(b"\n", b" "))
        history = self.client.get("/history")
        self.assertIn("已取消".encode(), history.data)
        self.assertIn("运行日志已保留，结果不完整。".encode(), history.data)
        again = self.client.post(
            "/api/jobs/{}/cancel".format(job_id),
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(again.status_code, 409)

        token = re.search(
            rb'name="csrf-token" content="([^"]+)"', self.client.get("/").data
        ).group(1).decode()
        finished = self.submit_job(self.client, token, ("megahit",)).json["job"]["id"]
        self.assertTrue(process_next_job(self.app))
        self.assertEqual(
            self.client.post(
                "/api/jobs/{}/cancel".format(finished),
                headers={"X-CSRF-Token": token},
            ).status_code,
            409,
        )
        failed = self.submit_job(self.client, token, ("spades",)).json["job"]["id"]
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (failed,))
            db.commit()
        self.assertEqual(
            self.client.post(
                "/api/jobs/{}/cancel".format(failed),
                headers={"X-CSRF-Token": token},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get("/api/jobs/{}".format(failed)).json["job"]["status"],
            "failed",
        )

    def test_cancel_signals_process_group_and_keeps_finished_output(self):
        self.pipeline.write_text(
            "import os, signal, sys, time, pathlib\n"
            "args = sys.argv\n"
            "out = pathlib.Path(args[args.index('--outdir') + 1])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'fastp').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'fastp' / 'report.html').write_text('fastp report')\n"
            "(out / 'quast' / 'spades').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'quast' / 'spades' / 'report.tsv').write_text('quast')\n"
            "(out / 'assemblies' / 'spades' / 'tmp').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'assemblies' / 'spades' / 'contigs.fasta').write_text('>contig')\n"
            "(out / 'assemblies' / 'spades' / 'tmp' / 'junk').write_text('keep')\n"
            "(out / 'assemblies' / 'megahit' / 'tmp').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'assemblies' / 'megahit' / 'tmp' / 'junk').write_text('temp')\n"
            "(out / 'assemblies' / 'megahit' / 'intermediate_contigs').mkdir(parents=True)\n"
            "(out / 'assemblies' / 'megahit' / 'intermediate_contigs' / 'part').write_text('temp')\n"
            "(out / 'logs').mkdir(parents=True, exist_ok=True)\n"
            "(out / 'logs' / 'spades.log').write_text('spades log')\n"
            "(out / 'run.log').write_text('pipeline log')\n"
            "(out / 'pipeline.pid').write_text(str(os.getpid()))\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    grand = os.fork()\n"
            "    if grand == 0:\n"
            "        def on_term(signum, _frame):\n"
            "            (out / 'grandchild.signal').write_text(str(signum))\n"
            "            os._exit(0)\n"
            "        signal.signal(signal.SIGTERM, on_term)\n"
            "        (out / 'grandchild.pid').write_text(str(os.getpid()))\n"
            "        (out / 'grandchild.ready').write_text('1')\n"
            "        deadline = time.time() + 30\n"
            "        while time.time() < deadline:\n"
            "            time.sleep(0.05)\n"
            "        os._exit(0)\n"
            "    def on_child(signum, _frame):\n"
            "        (out / 'child.signal').write_text(str(signum))\n"
            "        os._exit(0)\n"
            "    signal.signal(signal.SIGTERM, on_child)\n"
            "    (out / 'child.pid').write_text(str(os.getpid()))\n"
            "    (out / 'child.ready').write_text('1')\n"
            "    os.wait()\n"
            "    os._exit(0)\n"
            "limit = time.time() + 10\n"
            "while time.time() < limit and not ((out / 'child.ready').exists() and (out / 'grandchild.ready').exists()):\n"
            "    time.sleep(0.02)\n"
            "(out / 'ready').write_text('1')\n"
            "print('ready', flush=True)\n"
            "os.waitpid(child, 0)\n",
            encoding="utf-8",
        )
        token = self.login(self.client)
        job_id = self.submit_job(self.client, token, ("spades", "megahit")).json["job"]["id"]
        job_dir = self.app.config["JOBS_ROOT"] / job_id
        work = job_dir / "work"
        holder = {}

        def run_job():
            holder["processed"] = process_next_job(self.app)

        thread = threading.Thread(target=run_job)
        thread.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not (work / "ready").is_file():
                time.sleep(0.05)
            self.assertTrue((work / "ready").is_file(), "pipeline did not reach the grandchild")
            accepted = self.client.post(
                "/api/jobs/{}/cancel".format(job_id),
                headers={"X-CSRF-Token": token},
            )
            self.assertIn(accepted.status_code, (200, 202))
            thread.join(15)
        finally:
            if thread.is_alive() and (work / "pipeline.pid").is_file():
                try:
                    os.killpg(int((work / "pipeline.pid").read_text()), signal.SIGKILL)
                except OSError:
                    pass
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(holder.get("processed"))
        for name in ("child", "grandchild"):
            signal_path = work / "{}.signal".format(name)
            self.assertTrue(signal_path.is_file(), name)
            self.assertEqual(signal_path.read_text(encoding="utf-8"), str(signal.SIGTERM))
            pid = int((work / "{}.pid".format(name)).read_text(encoding="utf-8"))
            with self.assertRaises(OSError):
                os.kill(pid, 0)
        job = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(job["archive_available"])
        self.assertIsNone(job["progress"]["current"]["percent"])
        log_path = job_dir / "logs" / "job.log"
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("STATUS: queued", log_text)
        self.assertIn("STATUS: running", log_text)
        self.assertIn("STATUS: cancelling", log_text)
        self.assertIn("STATUS: cancelled", log_text)
        self.assertIn("ready", log_text)
        self.assertTrue((work / "fastp" / "report.html").is_file())
        self.assertTrue((work / "quast" / "spades" / "report.tsv").is_file())
        self.assertTrue((work / "assemblies" / "spades" / "contigs.fasta").is_file())
        self.assertTrue((work / "assemblies" / "spades" / "tmp" / "junk").is_file())
        self.assertTrue((work / "logs" / "spades.log").is_file())
        self.assertTrue((work / "run.log").is_file())
        self.assertFalse((work / "assemblies" / "megahit" / "tmp").exists())
        self.assertFalse((work / "assemblies" / "megahit" / "intermediate_contigs").exists())
        self.assertTrue((job_dir / "output").is_dir())
        self.assertFalse((self.app.config["ARCHIVE_ROOT"] / "{}.tar.gz".format(job_id)).exists())
        self.assertFalse(process_next_job(self.app))
        self.assertEqual(
            self.client.get("/api/jobs/{}".format(job_id)).json["job"]["status"],
            "cancelled",
        )
        second = self.client.post(
            "/api/jobs/{}/cancel".format(job_id),
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(second.status_code, 409)

    def test_stop_process_group_escalates_to_sigkill(self):
        script = self.root / "ignore_term.py"
        script.write_text(
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    grand = os.fork()\n"
            "    if grand == 0:\n"
            "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "        open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "        while True:\n"
            "            time.sleep(0.05)\n"
            "    open(sys.argv[2], 'w').write(str(os.getpid()))\n"
            "    os.wait()\n"
            "    os._exit(0)\n"
            "open(sys.argv[3], 'w').write('ready')\n"
            "os.wait()\n",
            encoding="utf-8",
        )
        grandchild = self.root / "grandchild.pid"
        child = self.root / "child.pid"
        ready = self.root / "ready"
        process = subprocess.Popen(
            [sys.executable, str(script), str(grandchild), str(child), str(ready)],
            start_new_session=True,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not (ready.is_file() and grandchild.is_file() and child.is_file()):
                time.sleep(0.02)
            self.assertTrue(grandchild.is_file())
            stop_process_group(process, grace_seconds=0.3)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            for path in (grandchild, child):
                with self.assertRaises(OSError):
                    os.kill(int(path.read_text(encoding="utf-8")), 0)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait(timeout=2)

    def test_existing_database_accepts_cancelled_status(self):
        data_root = self.root / "status-data"
        data_root.mkdir()
        legacy = data_root / "jobs.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript(
                """
                CREATE TABLE jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    assemblers TEXT NOT NULL,
                    threads INTEGER NOT NULL,
                    sample TEXT NOT NULL,
                    spades_kmers TEXT,
                    spades_careful INTEGER NOT NULL,
                    input_r1 TEXT NOT NULL,
                    input_r2 TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    archive_path TEXT,
                    log_path TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    started_at TEXT,
                    completed_at TEXT
                );
                """
            )
            db.execute(
                "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, "
                "sample, spades_careful, input_r1, input_r2, output_dir, log_path) "
                "VALUES ('legacy-job', 'alice', 'bacterial-assembly', 'queued', '[]', 1, "
                "'legacy', 0, 'r1.fastq', 'r2.fastq', 'output', 'job.log')"
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
        database = data_root / "database" / "jobs.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE jobs SET status = 'cancelled' WHERE id = 'legacy-job'")
            db.commit()
            row = db.execute(
                "SELECT status, cancel_requested FROM jobs WHERE id = 'legacy-job'"
            ).fetchone()
            columns = {record[1] for record in db.execute("PRAGMA table_info(jobs)")}
        self.assertEqual(row, ("cancelled", 0))
        self.assertIn("worker_pid", columns)


if __name__ == "__main__":
    unittest.main()
