import tempfile
import unittest
from pathlib import Path
from unittest import mock

from run_pipeline import ASSEMBLY_CONTIGS, run_assembler
from scripts.stage_record import StageRecord


class QuastFailureIsolationTests(unittest.TestCase):
    def test_quast_failure_does_not_fail_assembler_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            outdir = Path(temporary)
            (outdir / "logs").mkdir()
            assemblies = outdir / "assemblies" / "megahit"
            assemblies.mkdir(parents=True)
            contigs = assemblies / ASSEMBLY_CONTIGS["megahit"]
            contigs.write_text(">c\nACGTACGTACGT\n", encoding="utf-8")
            r1 = outdir / "r1.fq"
            r2 = outdir / "r2.fq"
            r1.write_text("@1\nACGT\n+\nIIII\n", encoding="utf-8")
            r2.write_text("@1\nACGT\n+\nIIII\n", encoding="utf-8")
            stages = StageRecord(outdir / "stages.json", ["megahit"])
            root_log = outdir / "run.log"
            root_log.write_text("", encoding="utf-8")

            def fake_run_script(script, arguments, *args, **kwargs):
                if script == "run_quast.py":
                    raise RuntimeError("quast boom")
                if script.startswith("run_"):
                    return None
                raise AssertionError(script)

            with mock.patch("run_pipeline.run_script", side_effect=fake_run_script):
                with mock.patch("run_pipeline.run_contig_filter") as filter_mock:
                    def write_filters(source, filtered_500, filtered_1000, *a, **k):
                        filtered_500.write_text(">c\nACGTACGTACGT\n", encoding="utf-8")
                        filtered_1000.write_text(">c\nACGTACGTACGT\n", encoding="utf-8")

                    filter_mock.side_effect = write_filters
                    name, ok = run_assembler(
                        "megahit",
                        outdir,
                        r1,
                        r2,
                        2,
                        None,
                        False,
                        root_log,
                        __import__("threading").Lock(),
                        stages,
                    )
            self.assertEqual(name, "megahit")
            self.assertTrue(ok)
            quast_states = {
                stage["id"]: stage["state"]
                for stage in stages.data["stages"]
                if ":quast:" in stage["id"]
            }
            self.assertTrue(quast_states)
            self.assertTrue(all(state == "failed" for state in quast_states.values()))
            self.assertEqual(
                next(
                    stage["state"]
                    for stage in stages.data["stages"]
                    if stage["id"] == "megahit"
                ),
                "done",
            )


if __name__ == "__main__":
    unittest.main()
