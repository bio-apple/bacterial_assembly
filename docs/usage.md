# 命令行用法

## 总控流水线

把仓库只读挂载到 `/opt/bacterial-assembly`，把工作目录挂载为容器当前目录：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --platform linux/amd64 \
  -v /path/to/bacterial_assembly:/opt/bacterial-assembly:ro \
  -v "$PWD:/work" -w /work \
  bacterial-assembly:latest \
  python3 /opt/bacterial-assembly/run_pipeline.py \
  --r1 reads_R1.fastq.gz \
  --r2 reads_R2.fastq.gz \
  --outdir results/isolate_01 \
  --sample isolate_01 \
  --assembler spades \
  --assembler megahit \
  --spades-kmers 21,33,55,77 \
  --spades-careful
```

查看参数：

```bash
docker run --rm \
  --platform linux/amd64 \
  -v /path/to/bacterial_assembly:/opt/bacterial-assembly:ro \
  bacterial-assembly:latest \
  python3 /opt/bacterial-assembly/run_pipeline.py --help
```

### 主要参数

- `--r1` / `--r2`：双端 FASTQ（`.fastq`、`.fq` 或其 gzip）
- `--outdir`：空结果目录
- `--sample`：样本名（默认 `sample`）
- `--assembler`：可重复；`spades`、`unicycler`、`shovill`、`megahit`；同方法不可重复
- `--spades-kmers` / `--spades-careful`：仅在选择 SPAdes 时生效
- `--job-id`：可选 UUID；Web worker 总会传入，命令行可省略

命令行、Web 表单和 API 均不接受手动线程数或内存上限。

## 执行顺序与退出码

1. fastp 过滤并生成报告  
2. 按 `--assembler` 顺序串行组装；失败不取消后续方法  
3. 某方法成功后，对其原始组装和非空长度筛选结果跑 QUAST  
4. 只汇总本次成功的原始组装  

写出 `assembly_outcome.json`（临时文件后原子替换）：

| 情况 | 退出码 | 声明状态 |
| --- | --- | --- |
| 全部组装方法成功 | 0 | `succeeded` |
| 至少一种成功且至少一种失败 | 3 | `partial_success` |
| 全部失败，或 fastp 等前置失败 | 1 | `failed` |

CLI 非零表示未全部成功。Web 将部分成功记为 `partial_success`（部分完成），不会仅因退出码非零记为 `failed`。

## 资源

总控先取主机逻辑 CPU（`os.cpu_count()`）和主机物理内存（`sysconf`），再读 cgroup v1/v2：CPU 取 quota 与 cpuset 中更紧的限制，内存取 `memory.max` / `memory.limit_in_bytes`（无限标记视为无限制）。有效上限是主机读数与 cgroup 限制的更小值；读不到 cgroup 时回退到主机读数。再对有效上限取 90%（CPU 至少 1 线程，内存至少 1 GiB），写入 `run.log`。

不是「宿主机物理内存的 90%」单独决定。`run_pipeline.py` 与 RQ Worker 写入 `_ASSEMBLY_ASSIGNED_THREADS` 与 `_ASSEMBLY_METHOD_MEMORY_GIB`；fastp 与各组装脚本读取这些配额。独立运行 QUAST 且无总控分配线程时，使用 `os.cpu_count()`。独立运行 fastp 或组装脚本且无总控分配时，自行按 cgroup/主机规则取 90% CPU/内存。

## 独立脚本

```bash
python3 scripts/run_fastp.py --help
python3 scripts/run_spades.py --help
python3 scripts/run_unicycler.py --help
python3 scripts/run_shovill.py --help
python3 scripts/run_megahit.py --help
python3 scripts/run_quast.py --help
python3 scripts/filter_contigs.py --help
python3 scripts/summarize_quast.py --help
```

可用 `--log` 和 `--versions-file`；省略时写在该步骤结果目录的上级目录。

组装方法参数见 [assembly_methods.md](assembly_methods.md)。结果说明见 [output.md](output.md)。
