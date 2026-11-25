import errno
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
import uuid
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
from web.errors import explain_job_failure
from web.fastq_check import records_accepted
from web.worker import (
    claim_next_job,
    process_next_job,
    publish_directory,
    read_process,
    recover_interrupted_jobs,
    stop_process_group,
    transition_job,
)


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
        self.fastq_check = self.root / "fastq_check.py"
        self.fastq_check.write_text(
            "print('Done processing 1 fastq records, no errors found')\n",
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
                "FASTQ_CHECK_COMMAND": [sys.executable, str(self.fastq_check)],
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
        self.assertIn("模块目录".encode(), module_page.data)
        self.assertIn("基因组分析".encode(), module_page.data)
        self.assertIn("细菌基因组组装".encode(), module_page.data)
        self.assertIn("模块版本 1.0".encode(), module_page.data)
        self.assertIn("Illumina 双端 FASTQ".encode(), module_page.data)
        self.assertIn("Shovill/SKESA".encode(), module_page.data)
        self.assertIn("已启用".encode(), module_page.data)
        self.assertIn("尚未运行".encode(), module_page.data)
        self.assertIn("空闲".encode(), module_page.data)
        self.assertIn(b'href="/"', module_page.data)
        self.assertNotIn(b"Planned", module_page.data)
        self.assertNotIn(b"Genome Annotation", module_page.data)
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
        self.assertIn("基因组分析".encode(), module_page.data)
        self.assertNotIn("后续分析模块".encode(), module_page.data.split("基因组分析".encode(), 1)[0])

    def test_module_directory_scopes_running_state_to_the_current_user(self):
        token = self.login(self.client)
        response = self.submit_job(self.client, token, ("spades",))
        job_id = response.json["job"]["id"]
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "UPDATE jobs SET status = 'running', started_at = created_at WHERE id = ?",
                (job_id,),
            )
            db.commit()

        page = self.client.get("/modules")
        text = page.get_data(as_text=True)
        self.assertIn("分析中 1", text)
        self.assertIn("已启用", text)
        self.assertNotIn("尚未运行", text)
        self.assertNotIn("空闲", text)
        self.assertNotIn("isolate_17", text)

        bob = self.app.test_client()
        self.login(bob, "bob", "bob-password")
        bob_page = bob.get("/modules").get_data(as_text=True)
        self.assertIn("尚未运行", bob_page)
        self.assertIn("空闲", bob_page)
        self.assertNotIn("分析中", bob_page)
        self.assertNotIn("isolate_17", bob_page)

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

    def test_fastq_record_check_requires_a_clean_log(self):
        self.assertTrue(records_accepted(0, "Done processing 2 fastq records, no errors found\nDone"))
        self.assertFalse(
            records_accepted(0, "Error found at readnumber: 1, linenumber 1\nDone")
        )
        self.assertFalse(records_accepted(1, "Done processing 2 fastq records, no errors found"))
        self.assertFalse(records_accepted(0, "Done"))

    def test_completed_upload_rejects_fastq_records(self):
        token = self.login(self.client)
        payload = b"@r1\nACGTACGT\n+\nIIIIIIII\n"
        upload = self.store_chunks(self.client, token, "reads_R1.fastq", "r1", payload)
        self.fastq_check.write_text(
            "import sys\n"
            "print('Error found at readnumber: 1, linenumber 1', file=sys.stderr)\n"
            "print('Done')\n",
            encoding="utf-8",
        )
        rejected = self.finish_upload(
            self.client, token, upload["id"], hashlib.sha256(payload).hexdigest()
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["code"], "fastq_invalid")
        self.assertIn("记录无法通过检查", rejected.json["error"])
        validated = self.app.config["UPLOAD_VALIDATED"] / upload["id"] / "read.fastq"
        self.assertFalse(validated.exists())
        status = self.client.get("/api/uploads/{}".format(upload["id"]))
        self.assertEqual(status.json["upload"]["status"], "failed")
        self.assertIsNone(status.json["upload"]["sha256"])
        created = self.client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler_order": '["spades"]',
                "assembler": ["spades"],
                "sample": "isolate_17",
                "r1_session": upload["id"],
                "r2_session": upload["id"],
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 400)

    def test_direct_upload_rejects_fastq_records_before_queue(self):
        token = self.login(self.client)
        self.fastq_check.write_text(
            "import sys\nprint('Error found at readnumber: 1', file=sys.stderr)\n",
            encoding="utf-8",
        )
        rejected = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["code"], "fastq_invalid")
        self.assertIn("记录无法通过检查", rejected.json["error"])
        self.assertFalse(any(self.app.config["JOBS_ROOT"].rglob("R1.fastq*")))

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
        self.assertIn("process_group_id", columns)
        self.assertIn("process_started_at", columns)

    def test_notices_follow_the_job_owner_through_worker_transitions(self):
        token = self.login(self.client)
        created = self.submit_job(self.client, token)
        self.assertEqual(created.status_code, 202, created.get_json())
        job_id = created.json["job"]["id"]
        own = self.client.get("/api/notices").json
        submitted = [item for item in own["notices"] if item["kind"] == "submitted"]
        self.assertEqual([item["job_id"] for item in submitted], [job_id])
        self.assertIn("/jobs/{}".format(job_id), submitted[0]["href"])
        self.assertGreaterEqual(own["unread"], 1)
        page = self.client.get("/")
        self.assertIn("实验室通知".encode(), page.data)

        bob = self.app.test_client()
        self.login(bob, "bob", "bob-password")
        self.assertEqual(bob.get("/api/notices").json["notices"], [])

        self.assertTrue(process_next_job(self.app))
        kinds = {
            item["kind"]
            for item in self.client.get("/api/notices").json["notices"]
            if item["job_id"] == job_id
        }
        self.assertIn("running", kinds)
        self.assertIn("succeeded", kinds)
        self.assertEqual(bob.get("/api/notices").json["notices"], [])

        self.pipeline.write_text("raise SystemExit(1)\n", encoding="utf-8")
        failed = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(failed.status_code, 202, failed.get_json())
        failed_id = failed.json["job"]["id"]
        self.assertTrue(process_next_job(self.app))
        failed_kinds = [
            item["kind"]
            for item in self.client.get("/api/notices").json["notices"]
            if item["job_id"] == failed_id
        ]
        self.assertIn("running", failed_kinds)
        self.assertIn("failed", failed_kinds)
        failed_job = self.client.get("/api/jobs/{}".format(failed_id)).json["job"]
        self.assertIn("软件运行错误", failed_job["error"])
        self.assertNotIn(failed_id, json.dumps(bob.get("/api/notices").json))

        marked = self.client.post("/api/notices/read", headers={"X-CSRF-Token": token})
        self.assertEqual(marked.status_code, 200)
        self.assertEqual(self.client.get("/api/notices").json["unread"], 0)
        self.assertEqual(bob.get("/api/notices").json["unread"], 0)

    def test_summary_failure_is_reported_as_a_report_error(self):
        self.pipeline.write_text(
            "import json, pathlib, sys\n"
            "args = sys.argv\n"
            "out = pathlib.Path(args[args.index('--outdir') + 1])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "(out / 'stages.json').write_text(json.dumps({'stages': ["
            "{'id': 'spades', 'kind': 'assembly', 'state': 'done'},"
            "{'id': 'comparison', 'kind': 'comparison', 'state': 'failed'}"
            "]}), encoding='utf-8')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        token = self.login(self.client)
        response = self.submit_job(self.client, token, ("spades",))
        job_id = response.json["job"]["id"]
        self.assertTrue(process_next_job(self.app))
        job = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("报告生成错误", job["error"])
        log = self.client.get("/api/jobs/{}/log".format(job_id)).json["text"]
        self.assertIn("WEB WORKER ERROR", log)
        self.assertNotIn("Traceback", job["error"])

    def test_disk_full_upload_returns_disk_message(self):
        token = self.login(self.client)
        enospc = OSError(errno.ENOSPC, "No space left on device")
        with patch("web.app.Path.mkdir", autospec=True, side_effect=enospc):
            response = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(response.status_code, 507)
        body = response.get_json()
        self.assertEqual(body["code"], "disk_full")
        self.assertIn("磁盘不足", body["message"])
        self.assertNotIn("Internal Server Error", response.get_data(as_text=True))
        notices = self.client.get("/api/notices").json["notices"]
        self.assertTrue(any(item["kind"] == "disk" for item in notices))

    def test_invalid_kmer_returns_parameter_message(self):
        token = self.login(self.client)
        response = self.client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "sample": "isolate_17",
                "spades_kmers": "22",
                "r1": (io.BytesIO(b"@r1\nACGT\n+\nIIII\n"), "reads_R1.fastq"),
                "r2": (io.BytesIO(b"@r2\nTGCA\n+\nIIII\n"), "reads_R2.fastq"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "parameter_invalid")
        self.assertIn("参数校验错误", response.json["message"])
        self.assertIn("参数校验错误", response.json["error"])
        self.assertNotIn("Internal Server Error", response.get_data(as_text=True))

    def test_unreadable_cpu_returns_resource_message(self):
        token = self.login(self.client)
        with patch("web.app.cpu_share", side_effect=RuntimeError("could not read cpu")):
            response = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "resource_unavailable")
        self.assertIn("资源不足", response.json["message"])
        code, message = explain_job_failure(-signal.SIGKILL, None, "", [], False)
        self.assertEqual(code, "resource_unavailable")
        self.assertIn("资源不足", message)

    def insert_owned_job(
        self,
        owner,
        sample,
        status,
        created_at,
        module_id="bacterial-assembly",
        comparison=None,
        fasta=None,
    ):
        job_id = str(uuid.uuid4())
        job_dir = self.app.config["JOBS_ROOT"] / job_id
        output = job_dir / "output"
        inputs = job_dir / "input"
        logs = job_dir / "logs"
        for directory in (output, inputs, logs):
            directory.mkdir(parents=True)
        r1 = inputs / "R1.fastq"
        r2 = inputs / "R2.fastq"
        r1.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
        r2.write_text("@r2\nTGCA\n+\nIIII\n", encoding="utf-8")
        log = logs / "job.log"
        log.write_text("STATUS: {}\n".format(status), encoding="utf-8")
        if comparison is not None:
            (output / "comparison.tsv").write_text(comparison, encoding="utf-8")
        if fasta is not None:
            (output / "contigs.fasta").write_text(fasta, encoding="utf-8")
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, sample, "
                "spades_kmers, spades_careful, input_r1, input_r2, output_dir, log_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, NULL, 1, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    owner,
                    module_id,
                    status,
                    json.dumps(["spades"]),
                    sample,
                    str(r1),
                    str(r2),
                    str(output),
                    str(log),
                    created_at,
                ),
            )
            db.commit()
        return job_id

    def test_history_sample_filter_stays_with_the_owner(self):
        self.login(self.client)
        alice_id = self.insert_owned_job(
            "alice", "AliceVisible", "succeeded", "2026-04-01T00:00:00Z"
        )
        bob_id = self.insert_owned_job(
            "bob", "ZZBobPrivate", "succeeded", "2026-04-02T00:00:00Z"
        )
        leaked = self.client.get("/history", query_string={"sample": "ZZBobPrivate"})
        self.assertEqual(leaked.status_code, 200)
        self.assertNotIn(bob_id.encode(), leaked.data)
        self.assertNotIn(alice_id.encode(), leaked.data)
        self.assertIn("没有符合条件的任务".encode(), leaked.data)
        self.assertIn("放宽样本名称".encode(), leaked.data)
        self.assertNotIn("还没有处理记录".encode(), leaked.data)
        matched = self.client.get("/history", query_string={"sample": "AliceVisible"})
        self.assertIn(alice_id.encode(), matched.data)
        self.assertNotIn(bob_id.encode(), matched.data)
        wrong_case = self.client.get("/history", query_string={"sample": "alicevisible"})
        self.assertNotIn(alice_id.encode(), wrong_case.data)
        self.assertIn("区分大小写".encode(), matched.data)
        self.assertIn("样本名称".encode(), matched.data)

    def test_history_status_and_date_filters(self):
        self.login(self.client)
        queued = self.insert_owned_job("alice", "day_queued", "queued", "2026-01-01T12:00:00Z")
        failed = self.insert_owned_job("alice", "day_failed", "failed", "2026-01-01T23:59:59Z")
        done = self.insert_owned_job("alice", "day_done", "succeeded", "2026-01-01T08:00:00Z")
        later = self.insert_owned_job("alice", "later_failed", "failed", "2026-02-01T00:00:00Z")
        cancelled = self.insert_owned_job(
            "alice", "old_cancelled", "cancelled", "2024-05-05T00:00:00Z"
        )
        running = self.insert_owned_job("alice", "day_running", "running", "2026-01-01T01:00:00Z")
        cancelling = self.insert_owned_job(
            "alice", "day_cancelling", "cancelling", "2026-01-01T02:00:00Z"
        )

        queued_page = self.client.get("/history", query_string={"status": "queued"})
        self.assertIn(queued.encode(), queued_page.data)
        self.assertIn("排队中".encode(), queued_page.data)
        self.assertNotIn(failed.encode(), queued_page.data)
        self.assertNotIn(done.encode(), queued_page.data)

        cancelled_page = self.client.get("/history", query_string={"status": "cancelled"})
        self.assertIn(cancelled.encode(), cancelled_page.data)
        self.assertIn("已取消".encode(), cancelled_page.data)
        self.assertNotIn(queued.encode(), cancelled_page.data)

        same_day = self.client.get(
            "/history",
            query_string={"status": "failed", "start": "2026-01-01", "end": "2026-01-01"},
        )
        self.assertIn(failed.encode(), same_day.data)
        self.assertNotIn(later.encode(), same_day.data)
        self.assertNotIn(done.encode(), same_day.data)
        self.assertIn(b"<strong>1</strong>", same_day.data)

        self.assertEqual(
            self.client.get("/history", query_string={"status": "not-a-status"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/history", query_string={"start": "2026-03-02", "end": "2026-03-01"}
            ).status_code,
            400,
        )
        self.assertIn(
            "日期范围不正确",
            self.client.get("/history", query_string={"start": "2026-13-40"}).get_data(as_text=True),
        )
        self.assertEqual(
            self.client.get("/history", query_string={"module": "missing-module"}).status_code,
            400,
        )
        module_page = self.client.get("/history", query_string={"module": "bacterial-assembly"})
        self.assertEqual(module_page.status_code, 200)
        self.assertIn(done.encode(), module_page.data)
        self.assertIn(running.encode(), module_page.data)
        self.assertIn(cancelling.encode(), module_page.data)

        for index in range(51):
            self.insert_owned_job(
                "alice", "batch_sample", "succeeded", "2026-06-01T00:00:{:02d}Z".format(index % 60)
            )
        self.insert_owned_job("alice", "other_marker", "succeeded", "2026-07-01T00:00:00Z")
        filtered = self.client.get("/history", query_string={"sample": "batch_sample", "page": 2})
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("共 51 条".encode(), filtered.data)
        self.assertIn("第 2 / 2 页".encode(), filtered.data)
        self.assertNotIn(b"other_marker", filtered.data)
        self.assertIn(b"sample=batch_sample", filtered.data)

    def test_rerun_creates_a_new_job_and_keeps_original_output(self):
        token = self.login(self.client)
        created = self.submit_job(self.client, token, ("spades",))
        self.assertEqual(created.status_code, 202)
        job_id = created.json["job"]["id"]
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "UPDATE jobs SET spades_kmers = '21,33', spades_careful = 1 WHERE id = ?",
                (job_id,),
            )
            db.commit()
            stored = db.execute(
                "SELECT input_r1, input_r2, output_dir FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        original_output = Path(stored[2])
        (original_output / "keep.txt").write_text("original-output", encoding="utf-8")
        original_reads = Path(stored[0]).read_bytes()

        rerun = self.client.post(
            "/api/jobs/{}/rerun".format(job_id),
            headers={"X-CSRF-Token": token, "Accept": "application/json"},
        )
        self.assertEqual(rerun.status_code, 202, rerun.get_data(as_text=True))
        new_id = rerun.json["job"]["id"]
        self.assertNotEqual(new_id, job_id)
        self.assertEqual(rerun.json["job"]["rerun_of"], job_id)
        self.assertEqual(rerun.json["job"]["status"], "queued")
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            row = db.execute(
                "SELECT rerun_of, status, sample, assemblers, spades_kmers, spades_careful, "
                "input_r1, input_r2, output_dir FROM jobs WHERE id = ?",
                (new_id,),
            ).fetchone()
        self.assertEqual(row[0], job_id)
        self.assertEqual(row[1], "queued")
        self.assertEqual(row[2], "isolate_17")
        self.assertEqual(json.loads(row[3]), ["spades"])
        self.assertEqual(row[4], "21,33")
        self.assertEqual(row[5], 1)
        self.assertNotEqual(row[6], stored[0])
        self.assertEqual(Path(stored[0]).read_bytes(), original_reads)
        self.assertEqual(Path(row[6]).read_bytes(), original_reads)
        self.assertEqual(Path(row[7]).read_bytes(), Path(stored[1]).read_bytes())
        self.assertEqual((original_output / "keep.txt").read_text(encoding="utf-8"), "original-output")
        self.assertFalse((Path(row[8]) / "keep.txt").exists())

        history = self.client.get("/history")
        self.assertIn("重新分析".encode(), history.data)
        self.assertIn("原任务 {}".format(job_id[:8]).encode(), history.data)
        self.assertIn("新任务 {}".format(new_id[:8]).encode(), history.data)
        self.assertIn("/jobs/{}".format(job_id).encode(), history.data)

        Path(stored[0]).unlink()
        denied = self.client.post(
            "/api/jobs/{}/rerun".format(job_id),
            headers={"X-CSRF-Token": token, "Accept": "application/json"},
        )
        self.assertEqual(denied.status_code, 409)
        self.assertIn("重新分析", denied.json["error"])
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual((original_output / "keep.txt").read_text(encoding="utf-8"), "original-output")

        data_root = self.root / "rerun-column"
        data_root.mkdir()
        legacy = data_root / "jobs.sqlite3"
        legacy_schema = SCHEMA.replace("    rerun_of TEXT,\n", "")
        self.assertNotIn("rerun_of", legacy_schema)
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript(legacy_schema)
            db.execute(
                "INSERT INTO jobs (id, owner, module_id, status, assemblers, threads, sample, "
                "spades_careful, input_r1, input_r2, output_dir, log_path) "
                "VALUES ('legacy-job', 'alice', 'bacterial-assembly', 'succeeded', '[]', 1, "
                "'kept-sample', 0, 'r1.fastq', 'r2.fastq', 'output', 'job.log')"
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
        with closing(sqlite3.connect(data_root / "database" / "jobs.sqlite3")) as db:
            migrated = db.execute(
                "SELECT sample, rerun_of FROM jobs WHERE id = 'legacy-job'"
            ).fetchone()
        self.assertEqual(migrated, ("kept-sample", None))

    def test_single_result_download_rejects_path_escape(self):
        token = self.login(self.client)
        job_id = self.insert_owned_job(
            "alice",
            "download_sample",
            "succeeded",
            "2026-05-01T00:00:00Z",
            comparison="Assembler\tN50\nspades\t10\n",
            fasta=">c1\nACGT\n",
        )
        output = self.app.config["JOBS_ROOT"] / job_id / "output"
        secret = self.root / "secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        (output / "linked.tsv").symlink_to(secret)

        comparison = self.client.get(
            "/api/jobs/{}/files".format(job_id), query_string={"name": "comparison.tsv"}
        )
        self.assertEqual(comparison.status_code, 200)
        self.assertIn(b"spades\t10", comparison.data)
        comparison.close()
        fasta = self.client.get(
            "/api/jobs/{}/files".format(job_id), query_string={"name": "contigs.fasta"}
        )
        self.assertEqual(fasta.status_code, 200)
        self.assertIn(b">c1", fasta.data)
        fasta.close()
        self.assertIn(
            "contigs.fasta".encode(),
            self.client.get("/history").data,
        )

        for name in (
            "../secret.txt",
            "/etc/passwd",
            "linked.tsv",
            "contigs.fasta/../../secret.txt",
            "..",
        ):
            escaped = self.client.get(
                "/api/jobs/{}/files".format(job_id), query_string={"name": name}
            )
            self.assertEqual(escaped.status_code, 400, name)
            self.assertNotIn(b"TOPSECRET", escaped.data)
        missing = self.client.get(
            "/api/jobs/{}/files".format(job_id), query_string={"name": "missing.fasta"}
        )
        self.assertEqual(missing.status_code, 404)
        bob = self.app.test_client()
        self.login(bob, "bob", "bob-password")
        self.assertEqual(
            bob.get(
                "/api/jobs/{}/files".format(job_id), query_string={"name": "comparison.tsv"}
            ).status_code,
            404,
        )

    def test_compare_reads_real_summary_files(self):
        self.login(self.client)
        left = self.insert_owned_job(
            "alice",
            "left_sample",
            "succeeded",
            "2020-06-06T00:00:00Z",
            comparison="Assembler\t# contigs\tN50\nspades\t1117\t4567\n",
        )
        right = self.insert_owned_job(
            "alice",
            "right_sample",
            "failed",
            "2020-06-07T00:00:00Z",
            comparison=(
                "Assembler\t# contigs\tN50\n"
                "spades\t2228\t8901\n"
                "megahit\t3339\t1357\n"
            ),
        )
        bare = self.insert_owned_job(
            "alice", "bare_sample", "succeeded", "2020-06-08T00:00:00Z"
        )
        queued = self.insert_owned_job(
            "alice",
            "queued_summary",
            "queued",
            "2020-06-09T00:00:00Z",
            comparison="Assembler\t# contigs\nspades\t1\n",
        )
        history = self.client.get("/history")
        self.assertIn('<option value="{}">'.format(left).encode(), history.data)
        self.assertIn('<option value="{}">'.format(right).encode(), history.data)
        self.assertNotIn('<option value="{}">'.format(bare).encode(), history.data)
        self.assertNotIn('<option value="{}">'.format(queued).encode(), history.data)

        compared = self.client.get("/history/compare", query_string={"left": left, "right": right})
        self.assertEqual(compared.status_code, 200)
        text = compared.get_data(as_text=True)
        self.assertIn("<td>1117</td>", text)
        self.assertIn("<td>2228</td>", text)
        self.assertIn("<td>3339</td>", text)
        self.assertIn("<td>4567</td>", text)
        self.assertIn("<td>8901</td>", text)
        self.assertIn("<td>—</td>", text)
        self.assertNotIn("1672.5", text)
        self.assertNotIn("<td>6734</td>", text)
        self.assertIn("# contigs", text)
        self.assertIn("N50", text)

        missing = self.client.get("/history/compare", query_string={"left": left, "right": bare})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("比较表", missing.get_data(as_text=True))
        unfinished = self.client.get(
            "/history/compare", query_string={"left": left, "right": queued}
        )
        self.assertEqual(unfinished.status_code, 400)
        same = self.client.get("/history/compare", query_string={"left": left, "right": left})
        self.assertEqual(same.status_code, 400)
        bob = self.app.test_client()
        self.login(bob, "bob", "bob-password")
        self.assertEqual(
            bob.get("/history/compare", query_string={"left": left, "right": right}).status_code,
            404,
        )

    def test_sample_batch_comparison_and_report_stay_with_the_owner(self):
        token = self.login(self.client)
        first = self.submit_job(self.client, token, ("spades",))
        second = self.client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler_order": '["megahit"]',
                "assembler": ["megahit"],
                "sample": "isolate_17",
                "r1": (io.BytesIO(b"@r1\nACGT\n+\nIIII\n"), "reads_R1.fastq.gz"),
                "r2": (io.BytesIO(b"@r2\nTGCA\n+\nIIII\n"), "reads_R2.fastq.gz"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        unique = self.client.post(
            "/api/jobs",
            data={
                "csrf_token": token,
                "assembler_order": '["spades"]',
                "assembler": ["spades"],
                "sample": "alice_only_strain",
                "r1": (io.BytesIO(b"@r1\nACGT\n+\nIIII\n"), "reads_R1.fastq.gz"),
                "r2": (io.BytesIO(b"@r2\nTGCA\n+\nIIII\n"), "reads_R2.fastq.gz"),
            },
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202, second.get_data(as_text=True))
        self.assertEqual(unique.status_code, 202, unique.get_data(as_text=True))
        first_id = first.json["job"]["id"]
        second_id = second.json["job"]["id"]
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            links = db.execute(
                "SELECT id, sample_id, sequencing_batch_id FROM jobs WHERE id IN (?, ?)",
                (first_id, second_id),
            ).fetchall()
            batches = db.execute(
                "SELECT id, r1_filename, r1_sha256 FROM sequencing_batches WHERE owner = ?",
                ("alice",),
            ).fetchall()
        by_id = {row[0]: row for row in links}
        self.assertEqual(by_id[first_id][1], by_id[second_id][1])
        self.assertNotEqual(by_id[first_id][2], by_id[second_id][2])
        self.assertIn(("reads_R1.fastq.gz", None), [(row[1], row[2]) for row in batches])
        alice_page = self.client.get("/")
        self.assertIn(b'value="alice_only_strain"', alice_page.data)
        self.assertIn(b'value="isolate_17"', alice_page.data)

        left = b"@r1\nACGTACGT\n+\nIIIIIIII\n"
        right = b"@r2\nTGCAAAAA\n+\nIIIIIIII\n"
        r1 = self.store_chunks(self.client, token, "lane_R1.fastq", "r1", left)
        r2 = self.store_chunks(self.client, token, "lane_R2.fastq", "r2", right)
        self.assertEqual(
            self.finish_upload(self.client, token, r1["id"], hashlib.sha256(left).hexdigest()).status_code,
            200,
        )
        self.assertEqual(
            self.finish_upload(self.client, token, r2["id"], hashlib.sha256(right).hexdigest()).status_code,
            200,
        )
        hashed = self.client.post(
            "/api/jobs",
            data={
                "assembler": ["spades"],
                "assembler_order": '["spades"]',
                "sample": "sha_strain",
                "r1_session": r1["id"],
                "r2_session": r2["id"],
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(hashed.status_code, 202, hashed.get_data(as_text=True))
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            stored_batch = db.execute(
                "SELECT r1_filename, r2_filename, r1_sha256, r2_sha256 FROM sequencing_batches "
                "WHERE owner = 'alice' AND r1_filename = 'lane_R1.fastq'"
            ).fetchone()
        self.assertEqual(stored_batch[0], "lane_R1.fastq")
        self.assertEqual(stored_batch[1], "lane_R2.fastq")
        self.assertEqual(stored_batch[2], hashlib.sha256(left).hexdigest())
        self.assertEqual(stored_batch[3], hashlib.sha256(right).hexdigest())

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            output = Path(
                db.execute("SELECT output_dir FROM jobs WHERE id = ?", (first_id,)).fetchone()[0]
            )
        (output / "keep.txt").write_text("original-output", encoding="utf-8")
        rerun = self.client.post(
            "/api/jobs/{}/rerun".format(first_id),
            headers={"X-CSRF-Token": token, "Accept": "application/json"},
        )
        self.assertEqual(rerun.status_code, 202, rerun.get_data(as_text=True))
        rerun_id = rerun.json["job"]["id"]
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            rerun_row = db.execute(
                "SELECT sample_id, sequencing_batch_id, output_dir FROM jobs WHERE id = ?",
                (rerun_id,),
            ).fetchone()
            batch_count = db.execute(
                "SELECT COUNT(*) FROM sequencing_batches WHERE sample_id = ?",
                (by_id[first_id][1],),
            ).fetchone()[0]
        self.assertEqual(rerun_row[0], by_id[first_id][1])
        self.assertEqual(rerun_row[1], by_id[first_id][2])
        self.assertNotEqual(rerun_row[2], str(output))
        self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "original-output")
        self.assertFalse((Path(rerun_row[2]) / "keep.txt").exists())
        self.assertEqual(batch_count, 2)

        bob = self.app.test_client()
        bob_token = self.login(bob, "bob", "bob-password")
        bob_job = bob.post(
            "/api/jobs",
            data={
                "csrf_token": bob_token,
                "assembler_order": '["spades"]',
                "assembler": ["spades"],
                "sample": "isolate_17",
                "r1": (io.BytesIO(b"@r1\nACGT\n+\nIIII\n"), "reads_R1.fastq.gz"),
                "r2": (io.BytesIO(b"@r2\nTGCA\n+\nIIII\n"), "reads_R2.fastq.gz"),
            },
            headers={"X-CSRF-Token": bob_token},
            content_type="multipart/form-data",
        )
        self.assertEqual(bob_job.status_code, 202, bob_job.get_data(as_text=True))
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            owners = db.execute(
                "SELECT owner, id FROM samples WHERE name = 'isolate_17' ORDER BY owner"
            ).fetchall()
        self.assertEqual([row[0] for row in owners], ["alice", "bob"])
        self.assertNotEqual(owners[0][1], owners[1][1])
        self.assertNotIn(b"alice_only_strain", bob.get("/").data)

        job_id = self.insert_owned_job(
            "alice", "kept_sample", "succeeded", "2026-08-01T00:00:00Z"
        )
        self.assertEqual(
            self.client.get("/jobs/{}/report".format(job_id)).status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/jobs/{}/reports/tsv".format(job_id)).status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/jobs/{}/reports/pdf".format(job_id)).status_code, 404
        )
        bare_page = self.client.get("/jobs/{}".format(job_id))
        self.assertIn("kept_sample".encode(), bare_page.data)
        self.assertIn("没有单独的测序数据记录".encode(), bare_page.data)
        self.assertIn("比较尚未就绪".encode(), bare_page.data)
        self.assertIn("重新分析".encode(), bare_page.data)
        self.assertIn(b'id="result-download"', bare_page.data)
        self.assertIn(b'id="job-log"', bare_page.data)
        self.assertIn("PDF 未提供".encode(), bare_page.data)
        self.assertIn(b'id="host-usage" hidden', bare_page.data)
        self.assertIn("不是本任务的历史曲线".encode(), bare_page.data)
        self.assertEqual(bare_page.get_data(as_text=True).count("推荐"), 1)

        output_dir = self.app.config["JOBS_ROOT"] / job_id / "output"
        comparison = (
            "Assembler\t# contigs\tTotal length\tLargest contig\tN50\tL50\tGC (%)\n"
            "spades\t10\t5000\t3000\t800\t2\t45.5\n"
            "megahit\t40\t9000\t1000\t2000\t4\t50\n"
            "shovill\t1\t1\t1\t99999\t1\t1\n"
        )
        (output_dir / "comparison.tsv").write_text(comparison, encoding="utf-8")
        (output_dir / "versions.txt").write_text("fastp\tfastp 0.23.4\n", encoding="utf-8")
        (output_dir / "run.log").write_text(
            "Threads per method: 3\nMemory per method: 7 GiB\n",
            encoding="utf-8",
        )
        fastp_dir = output_dir / "fastp"
        fastp_dir.mkdir()
        (fastp_dir / "kept_sample_fastp.json").write_text(
            json.dumps({"summary": {"before_filtering": {"total_reads": 18, "q20_rate": 0.25}}}),
            encoding="utf-8",
        )
        (output_dir / "stages.json").write_text(
            json.dumps(
                {
                    "stages": [
                        {
                            "id": "spades",
                            "label": "组装",
                            "state": "failed",
                            "detail": "spades exited 1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "UPDATE jobs SET assemblers = ?, error = ? WHERE id = ?",
                (
                    json.dumps(["spades", "unicycler", "megahit"]),
                    "pipeline exploded",
                    job_id,
                ),
            )
            db.commit()

        detail = self.client.get("/api/jobs/{}".format(job_id)).json["job"]["detail"]
        comparison_detail = detail["comparison"]
        self.assertEqual(
            [column["id"] for column in comparison_detail["columns"]],
            ["spades", "unicycler", "megahit"],
        )
        n50 = next(metric for metric in comparison_detail["metrics"] if metric["name"] == "N50")
        self.assertEqual([cell["text"] for cell in n50["cells"]], ["800", "无记录", "2000"])
        self.assertEqual(
            [(bar["id"], bar["width"]) for bar in comparison_detail["charts"][0]["bars"]],
            [("spades", 40), ("megahit", 100)],
        )
        self.assertNotIn("shovill", [bar["id"] for bar in comparison_detail["charts"][0]["bars"]])
        self.assertEqual(comparison_detail["missing"], ["unicycler"])
        self.assertEqual(comparison_detail["extra"], ["shovill"])
        self.assertEqual(detail["resources"]["threads"], 3)
        self.assertEqual(detail["resources"]["memory_gib"], 7)
        self.assertNotIn("best", json.dumps(detail))
        self.assertNotIn("rank", json.dumps(detail))

        page = self.client.get("/jobs/{}".format(job_id))
        text = page.get_data(as_text=True)
        self.assertIn("本次运行记录：每种方法 3 线程。", text)
        self.assertIn("本次运行记录：每种方法 7 GiB。", text)
        self.assertNotIn("任务记录的线程数", text)
        self.assertIn("无记录", text)
        self.assertNotIn("99999", text)
        self.assertLess(text.find(">SPAdes<"), text.find(">Unicycler<"))
        self.assertLess(text.find(">Unicycler<"), text.find(">MEGAHIT<"))
        self.assertIn('style="width: 40%"', text)
        self.assertEqual(text.count("推荐"), 1)
        self.assertIn("不要仅根据 N50 自动推荐最佳组装结果。", text)
        self.assertNotIn("<progress", text)

        report = self.client.get("/jobs/{}/report".format(job_id))
        self.assertEqual(report.status_code, 200)
        report_text = report.get_data(as_text=True)
        self.assertIn("fastp 0.23.4", report_text)
        self.assertNotIn("SPAdes 3.15.5", report_text)
        self.assertIn("过滤前总 reads", report_text)
        self.assertIn(">18<", report_text)
        self.assertIn("0.25", report_text)
        self.assertNotIn("过滤前 Q30 比例", report_text)
        self.assertNotIn("质量控制指标未记录", report_text)
        self.assertIn("pipeline exploded", report_text)
        self.assertIn("spades exited 1", report_text)
        self.assertNotIn("99999", report_text)
        self.assertEqual(report_text.count("推荐"), 1)
        self.assertIn("PDF 未提供", report_text)

        downloaded = self.client.get("/api/jobs/{}/reports/tsv".format(job_id))
        self.assertEqual(downloaded.status_code, 200)
        tsv = downloaded.get_data(as_text=True)
        downloaded.close()
        self.assertIn("assembly\tN50\tspades\t800", tsv)
        self.assertIn("assembly\t\tunicycler\t无记录", tsv)
        self.assertIn("version\tfastp\t\tfastp 0.23.4", tsv)
        self.assertIn("qc\t过滤前总 reads\t\t18", tsv)
        self.assertIn("anomaly\t任务错误\t\tpipeline exploded", tsv)
        self.assertNotIn("99999", tsv)
        self.assertNotIn("SPAdes 3.15.5", tsv)
        html = self.client.get("/api/jobs/{}/reports/html".format(job_id))
        self.assertEqual(html.status_code, 200)
        self.assertIn(b"fastp 0.23.4", html.data)
        html.close()
        self.assertEqual(bob.get("/jobs/{}/report".format(job_id)).status_code, 404)
        stolen = bob.get("/api/jobs/{}/reports/tsv".format(job_id))
        self.assertEqual(stolen.status_code, 404)
        self.assertNotIn(b"fastp 0.23.4", stolen.data)
        self.assertNotIn(b"pipeline exploded", stolen.data)

        secret = self.root / "report-secret.txt"
        secret.write_text("TOPSECRET-REPORT", encoding="utf-8")
        report_tsv = self.app.config["REPORTS_ROOT"] / job_id / "report.tsv"
        report_tsv.unlink()
        report_tsv.symlink_to(secret)
        blocked = self.client.get("/api/jobs/{}/reports/tsv".format(job_id))
        self.assertEqual(blocked.status_code, 404)
        self.assertNotIn(b"TOPSECRET-REPORT", blocked.data)


    def test_status_changes_only_from_the_allowed_state(self):
        token = self.login(self.client)
        created = self.submit_job(self.client, token, order=("spades",))
        self.assertEqual(created.status_code, 202)
        job_id = created.json["job"]["id"]
        queued = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertIsNone(queued["completed_at"])
        self.assertIsNone(queued["archived_at"])
        self.assertFalse(queued["archive_available"])

        claimed = claim_next_job(self.app.config["DATABASE"])
        self.assertEqual(claimed["id"], job_id)
        running = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["attempt"], 1)
        self.assertIsNone(running["completed_at"])
        self.assertTrue(running["worker_id"])
        self.assertTrue(running["heartbeat_at"])
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute("UPDATE jobs SET status = 'cancelling' WHERE id = ?", (job_id,))
            db.commit()
        self.assertFalse(
            transition_job(
                self.app.config["DATABASE"],
                job_id,
                "succeeded",
                ("running",),
                archive_path="/tmp/not-published.tar.gz",
            )
        )
        not_archived = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(not_archived["status"], "cancelling")
        self.assertIsNone(not_archived["archived_at"])
        self.assertIsNone(not_archived["completed_at"])
        self.assertFalse(not_archived["archive_available"])

        recover_interrupted_jobs(self.app.config["DATABASE"])
        cancelled_early = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(cancelled_early["status"], "cancelled")
        self.assertIsNotNone(cancelled_early["completed_at"])

        logged_cancel = self.insert_owned_job("alice", "logged_cancel", "running", "2026-01-02T00:00:00Z")
        log_path = self.app.config["JOBS_ROOT"] / logged_cancel / "logs" / "job.log"
        log_path.write_text("STATUS: running\nSTATUS: cancelled\n", encoding="utf-8")
        recover_interrupted_jobs(self.app.config["DATABASE"])
        cancelled = self.client.get("/api/jobs/{}".format(logged_cancel)).json["job"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNotNone(cancelled["completed_at"])
        self.assertIsNotNone(cancelled["cancel_requested_at"])

    def test_publish_leaves_the_final_directory_until_staging_finishes(self):
        source = self.root / "stage-work"
        destination = self.root / "stage-output"
        source.mkdir()
        (source / "contigs.fasta").write_text(">a\n", encoding="utf-8")
        destination.mkdir()
        (destination / "keep.txt").write_text("stay", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            publish_directory(source, destination)
        self.assertEqual((destination / "keep.txt").read_text(encoding="utf-8"), "stay")
        self.assertFalse((destination / "contigs.fasta").exists())
        self.assertEqual(
            (self.root / "stage-output.incoming" / "contigs.fasta").read_text(encoding="utf-8"),
            ">a\n",
        )

        ready = self.root / "ready-work"
        empty = self.root / "empty-output"
        ready.mkdir()
        empty.mkdir()
        (ready / "comparison.tsv").write_text("ok\n", encoding="utf-8")
        publish_directory(ready, empty)
        self.assertEqual((empty / "comparison.tsv").read_text(encoding="utf-8"), "ok\n")
        self.assertFalse((self.root / "empty-output.incoming").exists())
        self.assertFalse(any(ready.iterdir()))

    def test_recovery_checks_process_group_before_signalling(self):
        self.login(self.client)
        reused = self.insert_owned_job("alice", "reused_pid", "running", "2026-01-03T00:00:00Z")
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
            db.execute(
                "UPDATE jobs SET worker_pid = ?, process_group_id = ?, process_started_at = ? WHERE id = ?",
                (os.getpid(), os.getpgrp(), "not-the-live-start", reused),
            )
            db.commit()
        recover_interrupted_jobs(self.app.config["DATABASE"])
        os.kill(os.getpid(), 0)
        failed = self.client.get("/api/jobs/{}".format(reused)).json["job"]
        self.assertEqual(failed["status"], "failed")
        reused_log = (self.app.config["JOBS_ROOT"] / reused / "logs" / "job.log").read_text(encoding="utf-8")
        self.assertIn("did not signal it", reused_log)

        job_id = self.insert_owned_job("alice", "live_group", "running", "2026-01-04T00:00:00Z")
        script = self.root / "hold_group.py"
        grandchild = self.root / "held-grandchild.pid"
        ready = self.root / "held.ready"
        script.write_text(
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "grandchild, ready = sys.argv[-2], sys.argv[-1]\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    open(grandchild, 'w').write(str(os.getpid()))\n"
            "    while True:\n"
            "        time.sleep(0.05)\n"
            "open(ready, 'w').write('ready')\n"
            "os.wait()\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(script), "--job-id", job_id, str(grandchild), str(ready)],
            start_new_session=True,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not (ready.is_file() and grandchild.is_file()):
                time.sleep(0.02)
            self.assertTrue(grandchild.is_file())
            snap = read_process(process.pid)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["pgid"], process.pid)
            self.assertIn(job_id, snap["command"])
            with closing(sqlite3.connect(self.app.config["DATABASE"])) as db:
                db.execute(
                    "UPDATE jobs SET worker_pid = ?, process_group_id = ?, process_started_at = ? WHERE id = ?",
                    (process.pid, snap["pgid"], snap["start"], job_id),
                )
                db.commit()
            recover_interrupted_jobs(self.app.config["DATABASE"])
            process.wait(timeout=3)
            with self.assertRaises(OSError):
                os.kill(int(grandchild.read_text(encoding="utf-8")), 0)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait(timeout=2)
        finished = self.client.get("/api/jobs/{}".format(job_id)).json["job"]
        self.assertEqual(finished["status"], "failed")
        self.assertIsNone(finished["process_group_id"])


if __name__ == "__main__":
    unittest.main()
