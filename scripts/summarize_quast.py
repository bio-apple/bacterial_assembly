#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path


ASSEMBLERS = ("spades", "unicycler", "shovill", "megahit")
METRICS = (
    "# contigs",
    "Total length",
    "Largest contig",
    "N50",
    "L50",
    "GC (%)",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize per-assembler QUAST reports")
    parser.add_argument("--outdir", required=True, help="Pipeline result directory")
    parser.add_argument(
        "--assembler",
        choices=ASSEMBLERS,
        action="append",
        required=True,
        help="Assembler to include; repeat to select multiple",
    )
    args = parser.parse_args()
    if len(set(args.assembler)) != len(args.assembler):
        parser.error("each assembler may be summarized only once")
    outdir = Path(args.outdir)
    output = outdir / "comparison.tsv"
    try:
        with output.open("w", newline="", encoding="utf-8") as summary_file:
            writer = csv.writer(summary_file, delimiter="\t", lineterminator="\n")
            writer.writerow(("Assembler",) + METRICS)
            for assembler in args.assembler:
                report_path = outdir / "quast" / assembler / "report.tsv"
                if not report_path.is_file():
                    raise ValueError("QUAST report not found: {}".format(report_path))
                with report_path.open(newline="", encoding="utf-8") as report_file:
                    metrics = {
                        row[0]: row[1]
                        for row in csv.reader(report_file, delimiter="\t")
                        if len(row) > 1
                    }
                missing = [metric for metric in METRICS if metric not in metrics]
                if missing:
                    raise ValueError(
                        "{} is missing metrics: {}".format(
                            report_path, ", ".join(missing)
                        )
                    )
                writer.writerow((assembler,) + tuple(metrics[key] for key in METRICS))
    except (OSError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    print("QUAST summary: {}".format(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
