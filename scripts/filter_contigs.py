#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path


def write_filtered(
    source: Path,
    destination: Path,
    threshold: int,
) -> tuple[int, int]:
    count = 0
    bases = 0
    header = None
    sequence_lines = []

    def write_record(output_file) -> None:
        nonlocal count, bases
        if header is None:
            return
        length = sum(len(line.strip()) for line in sequence_lines)
        if length > threshold:
            output_file.write(header + "\n")
            for line in sequence_lines:
                output_file.write(line + "\n")
            count += 1
            bases += length

    with source.open(encoding="utf-8") as input_file, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as output_file:
        for raw_line in input_file:
            line = raw_line.rstrip("\r\n")
            if line.startswith(">"):
                write_record(output_file)
                header = line
                sequence_lines = []
            elif line.strip():
                if header is None:
                    raise ValueError("FASTA sequence found before the first header")
                sequence_lines.append(line)
        write_record(output_file)

    return count, bases


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write contigs strictly longer than 500 bp and 1000 bp"
    )
    parser.add_argument("--input", required=True, help="Assembly FASTA")
    parser.add_argument("--gt500", required=True, help="Output FASTA for contigs >500 bp")
    parser.add_argument(
        "--gt1000", required=True, help="Output FASTA for contigs >1000 bp"
    )
    args = parser.parse_args()
    source = Path(args.input)
    outputs = (Path(args.gt500), Path(args.gt1000))

    try:
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("input FASTA is missing or empty: {}".format(source))
        resolved = [source.resolve(), *(path.resolve() for path in outputs)]
        if resolved[0] in resolved[1:] or resolved[1] == resolved[2]:
            raise ValueError("input and output FASTA paths must be different")
        for output in outputs:
            if output.exists():
                raise ValueError("output FASTA already exists: {}".format(output))
            output.parent.mkdir(parents=True, exist_ok=True)

        for output, threshold in zip(outputs, (500, 1000)):
            count, bases = write_filtered(source, output, threshold)
            if count:
                print(
                    "> {} bp: kept {} contigs ({} bp) in {}".format(
                        threshold, count, bases, output
                    )
                )
            else:
                print(
                    "No contigs > {} bp; created empty {}".format(
                        threshold, output
                    )
                )
    except (OSError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
