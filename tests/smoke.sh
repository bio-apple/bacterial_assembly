#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

MOCKS="$TMP/bin"
mkdir -p "$MOCKS"
cat > "$MOCKS/fastp" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "fastp test-version"; exit 0; fi
printf 'fastp %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out1) out1=$2; shift 2 ;;
        --out2) out2=$2; shift 2 ;;
        --json) json=$2; shift 2 ;;
        --html) html=$2; shift 2 ;;
        *) shift ;;
    esac
done
mkdir -p "$(dirname "$out1")"
printf '@filtered/1\nACGT\n+\nIIII\n' > "$out1"
printf '@filtered/2\nTGCA\n+\nIIII\n' > "$out2"
printf '{"summary":{}}\n' > "$json"
printf '<html></html>\n' > "$html"
MOCK
cat > "$MOCKS/spades.py" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "SPAdes test-version"; exit 0; fi
printf 'spades %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
if [[ -n "${MOCK_PARALLEL_TRACE:-}" ]]; then
    python3 - "$MOCK_PARALLEL_TRACE" spades start <<'TRACE'
import sys, time
with open(sys.argv[1], "a") as stream:
    stream.write("{} {} {:.9f}\n".format(sys.argv[2], sys.argv[3], time.monotonic()))
TRACE
    sleep 0.3
fi
if [[ "${MOCK_FAIL_ASSEMBLER:-}" == spades ]]; then exit 1; fi
mkdir -p "$out"
if [[ -n "${MOCK_ASSEMBLY_FASTA:-}" ]]; then
    cp "$MOCK_ASSEMBLY_FASTA" "$out/contigs.fasta"
else
    printf '>contig\nACGT\n' > "$out/contigs.fasta"
fi
if [[ -n "${MOCK_PARALLEL_TRACE:-}" ]]; then
    python3 - "$MOCK_PARALLEL_TRACE" spades end <<'TRACE'
import sys, time
with open(sys.argv[1], "a") as stream:
    stream.write("{} {} {:.9f}\n".format(sys.argv[2], sys.argv[3], time.monotonic()))
TRACE
fi
MOCK
cat > "$MOCKS/micromamba" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "run" && "$2" == "-n" && "$3" == "unicycler" ]]; then
    shift 3
    exec "$@"
fi
echo "Unexpected micromamba invocation: $*" >&2
exit 1
MOCK
cat > "$MOCKS/unicycler" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "Unicycler test-version"; exit 0; fi
for arg in "$@"; do
    [[ "$arg" != "-l" ]] || { echo "Unicycler must not receive long reads" >&2; exit 1; }
done
printf 'unicycler %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
mkdir -p "$out"
if [[ -n "${MOCK_ASSEMBLY_FASTA:-}" ]]; then
    cp "$MOCK_ASSEMBLY_FASTA" "$out/assembly.fasta"
else
    printf '>contig\nACGT\n' > "$out/assembly.fasta"
fi
MOCK
cat > "$MOCKS/shovill" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "Shovill test-version"; exit 0; fi
printf 'shovill %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in --outdir) out=$2; shift 2 ;; *) shift ;; esac
done
mkdir -p "$out"
if [[ -n "${MOCK_ASSEMBLY_FASTA:-}" ]]; then
    cp "$MOCK_ASSEMBLY_FASTA" "$out/contigs.fa"
else
    printf '>contig\nACGT\n' > "$out/contigs.fa"
fi
MOCK
cat > "$MOCKS/skesa" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "SKESA test-version"; exit 0; fi
exit 1
MOCK
cat > "$MOCKS/megahit" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "MEGAHIT test-version"; exit 0; fi
printf 'megahit %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
if [[ -n "${MOCK_PARALLEL_TRACE:-}" ]]; then
    python3 - "$MOCK_PARALLEL_TRACE" megahit start <<'TRACE'
import sys, time
with open(sys.argv[1], "a") as stream:
    stream.write("{} {} {:.9f}\n".format(sys.argv[2], sys.argv[3], time.monotonic()))
TRACE
    sleep 0.3
fi
mkdir -p "$out"
if [[ -n "${MOCK_ASSEMBLY_FASTA:-}" ]]; then
    cp "$MOCK_ASSEMBLY_FASTA" "$out/final.contigs.fa"
else
    printf '>contig\nACGT\n' > "$out/final.contigs.fa"
fi
if [[ -n "${MOCK_PARALLEL_TRACE:-}" ]]; then
    python3 - "$MOCK_PARALLEL_TRACE" megahit end <<'TRACE'
import sys, time
with open(sys.argv[1], "a") as stream:
    stream.write("{} {} {:.9f}\n".format(sys.argv[2], sys.argv[3], time.monotonic()))
TRACE
fi
MOCK
cat > "$MOCKS/quast.py" <<'MOCK'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then echo "QUAST test-version"; exit 0; fi
printf 'quast %s\n' "$*" >> "$MOCK_CALL_LOG"
while [[ $# -gt 0 ]]; do
    case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac
done
mkdir -p "$out"
printf 'Assembly\tassembly\n# contigs\t1\nTotal length\t4\nLargest contig\t4\nN50\t4\nL50\t1\nGC (%%)\t50.0\n' \
    > "$out/report.tsv"
MOCK
chmod +x "$MOCKS/"*

printf '@read/1\nACGT\n+\nIIII\n' > "$TMP/r1.fastq"
printf '@read/2\nTGCA\n+\nIIII\n' > "$TMP/r2.fastq"
export PATH="$MOCKS:$PATH"
export MOCK_CALL_LOG="$TMP/calls.log"
export PYTHONDONTWRITEBYTECODE=1

python3 - "$ROOT" <<'PY'
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
for path in [root / "run_pipeline.py", *sorted((root / "scripts").glob("*.py"))]:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
python3 "$ROOT/run_pipeline.py" --help >/dev/null
python3 "$ROOT/scripts/run_fastp.py" --help >/dev/null
python3 "$ROOT/scripts/run_spades.py" --help >/dev/null
python3 "$ROOT/scripts/run_unicycler.py" --help >/dev/null
python3 "$ROOT/scripts/run_shovill.py" --help >/dev/null
python3 "$ROOT/scripts/run_megahit.py" --help >/dev/null
python3 "$ROOT/scripts/run_quast.py" --help >/dev/null
python3 "$ROOT/scripts/summarize_quast.py" --help >/dev/null
python3 "$ROOT/scripts/filter_contigs.py" --help >/dev/null
for runner in run_pipeline.py scripts/run_fastp.py scripts/run_spades.py \
    scripts/run_unicycler.py scripts/run_shovill.py scripts/run_megahit.py \
    scripts/run_quast.py; do
    if python3 "$ROOT/$runner" --help 2>&1 | grep -Eq -- '--threads|--memory'; then
        echo "$runner must not expose manual resource settings" >&2
        exit 1
    fi
done

if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" \
    --r2 "$TMP/r2.fastq" --outdir "$TMP/no-assembler" 2>/dev/null; then
    echo "Expected at least one assembler to be required" >&2
    exit 1
fi
if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" \
    --r2 "$TMP/r2.fastq" --outdir "$TMP/invalid" --assembler spades --threads 2 2>/dev/null; then
    echo "Expected manual thread count to be rejected" >&2
    exit 1
fi
if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" \
    --r2 "$TMP/r2.fastq" --outdir "$TMP/invalid-kmer" --assembler spades \
    --spades-kmers 22 2>/dev/null; then
    echo "Expected invalid k-mer to fail" >&2
    exit 1
fi
if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" \
    --r2 "$TMP/r2.fastq" --outdir "$TMP/invalid-memory" --assembler shovill \
    --memory 7 2>/dev/null; then
    echo "Expected manual memory setting to be rejected" >&2
    exit 1
fi
mkdir -p "$TMP/non-empty"
touch "$TMP/non-empty/keep.txt"
if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" \
    --r2 "$TMP/r2.fastq" --outdir "$TMP/non-empty" --assembler spades 2>/dev/null; then
    echo "Expected a non-empty output directory to be rejected" >&2
    exit 1
fi
[[ -f "$TMP/non-empty/keep.txt" ]]

python3 - "$TMP/boundary.fasta" <<'PY'
from pathlib import Path
import sys

records = ((500, "exact500"), (501, "plus500"), (1000, "exact1000"), (1001, "plus1000"))
with Path(sys.argv[1]).open("w") as fasta:
    for length, name in records:
        fasta.write(">{}\n".format(name))
        sequence = "A" * length
        for start in range(0, length, 80):
            fasta.write(sequence[start:start + 80] + "\n")
PY
python3 "$ROOT/scripts/filter_contigs.py" --input "$TMP/boundary.fasta" \
    --gt500 "$TMP/boundary/gt500.fasta" --gt1000 "$TMP/boundary/gt1000.fasta" \
    > "$TMP/filter.log"
python3 - "$TMP/boundary/gt500.fasta" "$TMP/boundary/gt1000.fasta" <<'PY'
from pathlib import Path
import sys

def headers_and_lengths(path):
    records = []
    header = None
    length = 0
    for line in Path(path).read_text().splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, length))
            header, length = line[1:], 0
        else:
            length += len(line.strip())
    if header is not None:
        records.append((header, length))
    return records

assert headers_and_lengths(sys.argv[1]) == [
    ("plus500", 501), ("exact1000", 1000), ("plus1000", 1001)
]
assert headers_and_lengths(sys.argv[2]) == [("plus1000", 1001)]
PY

export MOCK_CALL_LOG="$TMP/pipeline-calls.log"
export MOCK_ASSEMBLY_FASTA="$TMP/boundary.fasta"
export MOCK_PARALLEL_TRACE="$TMP/parallel-trace.log"
python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" --r2 "$TMP/r2.fastq" \
    --outdir "$TMP/run" --sample isolate_1 \
    --assembler megahit --assembler spades \
    --spades-kmers 21,33 --spades-careful > "$TMP/run.log"
for path in \
    assemblies/spades/contigs.fasta \
    assemblies/spades/contigs_gt500bp.fasta \
    assemblies/spades/contigs_gt1000bp.fasta \
    assemblies/megahit/final.contigs.fa \
    assemblies/megahit/contigs_gt500bp.fasta \
    assemblies/megahit/contigs_gt1000bp.fasta
do
    [[ -s "$TMP/run/$path" ]]
done
for assembler in megahit spades; do
    [[ -s "$TMP/run/quast/$assembler/report.tsv" ]]
    [[ -s "$TMP/run/quast/$assembler/gt500bp/report.tsv" ]]
    [[ -s "$TMP/run/quast/$assembler/gt1000bp/report.tsv" ]]
done
[[ ! -e "$TMP/run/assemblies/unicycler" ]]
[[ ! -e "$TMP/run/assemblies/shovill" ]]
[[ -s "$TMP/run/fastp/isolate_1_fastp.json" ]]
[[ -s "$TMP/run/fastp/isolate_1_fastp.html" ]]
python3 - "$TMP/pipeline-calls.log" "$TMP/parallel-trace.log" "$ROOT" <<'PY'
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, sys.argv[3])
from scripts.common import cpu_share, memory_share_gib

cpus = os.cpu_count() or 1
expected_method_threads = str(cpu_share(cpus))
expected_memory = str(memory_share_gib())
expected_memory_bytes = str(memory_share_gib() * 1024 ** 3)
for line in Path(sys.argv[1]).read_text().splitlines():
    name, *args = shlex.split(line)
    if name == "fastp":
        observed = args[args.index("--thread") + 1]
        assert observed == str(cpus), (name, observed, cpus)
        continue
    elif name == "spades":
        observed = args[args.index("-t") + 1]
        assert args[args.index("-m") + 1] == expected_memory
    elif name == "megahit":
        observed = args[args.index("--num-cpu-threads") + 1]
        assert args[args.index("--memory") + 1] == expected_memory_bytes
    elif name == "unicycler":
        observed = args[args.index("-t") + 1]
        option_at = args.index("--spades_options")
        assert args[option_at + 1 : option_at + 3] == ["-m", expected_memory]
    elif name == "shovill":
        observed = args[args.index("--cpus") + 1]
        assert args[args.index("--ram") + 1] == expected_memory
    elif name == "quast":
        observed = args[args.index("--threads") + 1]
    else:
        continue
    assert observed == expected_method_threads, (
        name, observed, expected_method_threads
    )

events = {}
for line in Path(sys.argv[2]).read_text().splitlines():
    name, phase, timestamp = line.split()
    events.setdefault(name, {})[phase] = float(timestamp)
assert events["megahit"]["end"] <= events["spades"]["start"]
PY
[[ -s "$TMP/run/comparison.tsv" ]]
grep -q $'SPAdes\tSPAdes test-version' "$TMP/run/versions.txt"
[[ $(wc -l < "$TMP/run/comparison.tsv" | tr -d ' ') -eq 3 ]]
[[ $(sed -n '2p' "$TMP/run/comparison.tsv" | cut -f1) == megahit ]]
[[ $(sed -n '3p' "$TMP/run/comparison.tsv" | cut -f1) == spades ]]
python3 - "$MOCK_CALL_LOG" "$TMP/run/fastp/isolate_1_R1.fastq.gz" <<'PY'
from collections import Counter
from pathlib import Path
import sys

lines = Path(sys.argv[1]).read_text().splitlines()
counts = Counter(line.split()[0] for line in lines)
assert counts == {"fastp": 1, "megahit": 1, "spades": 1, "quast": 6}, counts
assembly_lines = [line for line in lines if line.startswith(("megahit ", "spades "))]
assert all(sys.argv[2] in line for line in assembly_lines), lines
assert all(sys.argv[2] not in line for line in lines if line.startswith("quast ")), lines
assert any("final.contigs.fa" in line for line in lines if line.startswith("quast "))
assert any("contigs_gt500bp.fasta" in line for line in lines if line.startswith("quast "))
assert any("contigs_gt1000bp.fasta" in line for line in lines if line.startswith("quast "))
PY

unset MOCK_PARALLEL_TRACE
export MOCK_CALL_LOG="$TMP/partial-calls.log"
export MOCK_FAIL_ASSEMBLER=spades
if python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" --r2 "$TMP/r2.fastq" \
    --outdir "$TMP/partial" --assembler spades --assembler megahit \
    > "$TMP/partial.log"; then
    echo "Expected failed SPAdes to fail the aggregate task" >&2
    exit 1
fi
unset MOCK_FAIL_ASSEMBLER
[[ -s "$TMP/partial/comparison.tsv" ]]
grep -q $'megahit\t' "$TMP/partial/comparison.tsv"
! grep -q $'spades\t' "$TMP/partial/comparison.tsv"
grep -q 'Method complete' "$TMP/partial/run.log"
grep -q 'assembler methods failed: spades' "$TMP/partial/run.log"

export MOCK_CALL_LOG="$TMP/empty-filter-calls.log"
export MOCK_ASSEMBLY_FASTA="$TMP/only-501.fasta"
python3 - "$TMP/only-501.fasta" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(">contig501\n" + "A" * 501 + "\n")
PY
python3 "$ROOT/run_pipeline.py" --r1 "$TMP/r1.fastq" --r2 "$TMP/r2.fastq" \
    --outdir "$TMP/empty-filter" --assembler unicycler \
    > "$TMP/empty-filter.log"
[[ -s "$TMP/empty-filter/assemblies/unicycler/contigs_gt500bp.fasta" ]]
[[ ! -s "$TMP/empty-filter/assemblies/unicycler/contigs_gt1000bp.fasta" ]]
[[ -s "$TMP/empty-filter/quast/unicycler/report.tsv" ]]
[[ -s "$TMP/empty-filter/quast/unicycler/gt500bp/report.tsv" ]]
[[ ! -e "$TMP/empty-filter/quast/unicycler/gt1000bp" ]]
grep -q 'No contigs > 1000 bp' "$TMP/empty-filter/run.log"
grep -q 'Skipping QUAST for unicycler >1000 bp' "$TMP/empty-filter/run.log"

STANDALONE="$TMP/standalone"
export MOCK_CALL_LOG="$TMP/standalone-calls.log"
unset MOCK_ASSEMBLY_FASTA
python3 "$ROOT/scripts/run_fastp.py" --r1 "$TMP/r1.fastq" --r2 "$TMP/r2.fastq" \
    --outdir "$STANDALONE/fastp" --sample standalone \
    --log "$STANDALONE/run.log" --versions-file "$STANDALONE/versions.txt" >/dev/null
FILTERED_R1="$STANDALONE/fastp/standalone_R1.fastq.gz"
FILTERED_R2="$STANDALONE/fastp/standalone_R2.fastq.gz"
python3 "$ROOT/scripts/run_spades.py" --r1 "$FILTERED_R1" --r2 "$FILTERED_R2" \
    --outdir "$STANDALONE/assemblies/spades" \
    --kmers 21,33 --careful --log "$STANDALONE/run.log" \
    --versions-file "$STANDALONE/versions.txt" >/dev/null
python3 "$ROOT/scripts/run_unicycler.py" --r1 "$FILTERED_R1" --r2 "$FILTERED_R2" \
    --outdir "$STANDALONE/assemblies/unicycler" \
    --log "$STANDALONE/run.log" --versions-file "$STANDALONE/versions.txt" >/dev/null
python3 "$ROOT/scripts/run_shovill.py" --r1 "$FILTERED_R1" --r2 "$FILTERED_R2" \
    --outdir "$STANDALONE/assemblies/shovill" \
    --log "$STANDALONE/run.log" --versions-file "$STANDALONE/versions.txt" >/dev/null
python3 "$ROOT/scripts/run_megahit.py" --r1 "$FILTERED_R1" --r2 "$FILTERED_R2" \
    --outdir "$STANDALONE/assemblies/megahit" \
    --log "$STANDALONE/run.log" --versions-file "$STANDALONE/versions.txt" >/dev/null
python3 "$ROOT/scripts/run_quast.py" \
    --contigs "$STANDALONE/assemblies/spades/contigs.fasta" \
    --outdir "$STANDALONE/quast/spades" \
    --log "$STANDALONE/run.log" --versions-file "$STANDALONE/versions.txt" >/dev/null
python3 "$ROOT/scripts/summarize_quast.py" --outdir "$STANDALONE" \
    --assembler spades >/dev/null
[[ -s "$STANDALONE/quast/spades/report.tsv" ]]
[[ $(wc -l < "$STANDALONE/comparison.tsv" | tr -d ' ') -eq 2 ]]
python3 - "$TMP/standalone-calls.log" "$ROOT" <<'PY'
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, sys.argv[2])
from scripts.common import cpu_share, memory_share_gib

full_threads = str(os.cpu_count() or 1)
method_threads = str(cpu_share())
expected_memory = str(memory_share_gib())
expected_memory_bytes = str(memory_share_gib() * 1024 ** 3)
observed_tools = set()
for line in Path(sys.argv[1]).read_text().splitlines():
    name, *args = shlex.split(line)
    if name == "fastp":
        observed = args[args.index("--thread") + 1]
        expected = full_threads
    elif name == "spades":
        observed = args[args.index("-t") + 1]
        expected = method_threads
        assert args[args.index("-m") + 1] == expected_memory
    elif name == "unicycler":
        observed = args[args.index("-t") + 1]
        expected = method_threads
        option_at = args.index("--spades_options")
        assert args[option_at + 1 : option_at + 3] == ["-m", expected_memory]
    elif name == "shovill":
        observed = args[args.index("--cpus") + 1]
        expected = method_threads
        assert args[args.index("--ram") + 1] == expected_memory
    elif name == "megahit":
        observed = args[args.index("--num-cpu-threads") + 1]
        expected = method_threads
        assert args[args.index("--memory") + 1] == expected_memory_bytes
    elif name == "quast":
        observed = args[args.index("--threads") + 1]
        expected = full_threads
    else:
        continue
    assert observed == expected, (name, observed, expected)
    observed_tools.add(name)
assert observed_tools == {"fastp", "spades", "unicycler", "shovill", "megahit", "quast"}
PY

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$ROOT/tests"

echo "Pipeline smoke tests passed."
