# 组装方法与参数

可选方法：`spades`、`unicycler`、`shovill`、`megahit`。页面上的「组装器」即组装方法。至少选择一种；同一任务内不可重复。按选择（或 `--assembler`）顺序串行执行，共用同一份 fastp 过滤后 reads。

## SPAdes

- 脚本：`scripts/run_spades.py`
- 线程 / 内存：`-t`、`-m`（GiB）
- `--spades-kmers`：逗号分隔奇数，最大 127；省略则用 SPAdes 默认
- `--spades-careful`：仅在选择 SPAdes 时生效
- 成功时输出：`assemblies/spades/contigs.fasta`

## Unicycler

- 脚本：`scripts/run_unicycler.py`
- 运行环境：镜像中的 `unicycler`（含 SPAdes 4.0.0）
- 仅 Illumina 短读：传入 `-1`/`-2`，不传长读参数 `-l`
- 线程 / 内存：`-t`；通过 `--spades_options` 传入 `-m`
- 成功时输出：`assemblies/unicycler/assembly.fasta`

## Shovill

- 脚本：`scripts/run_shovill.py`
- 显式 `--assembler skesa`
- 线程 / 内存：`--cpus`、`--ram`（GiB）
- 成功时输出：`assemblies/shovill/contigs.fa`

## MEGAHIT

- 脚本：`scripts/run_megahit.py`
- 线程 / 内存：`--num-cpu-threads`、`--memory`（字节）
- 成功时输出：`assemblies/megahit/final.contigs.fa`

## 长度筛选与 QUAST

方法成功后生成 `contigs_gt500bp.fasta` 与 `contigs_gt1000bp.fasta`（严格大于阈值，可为空）。QUAST 评估原始组装和非空筛选结果；空筛选跳过 QUAST。

不根据 N50 自动推荐最佳组装。Web 任务详情可在「原始组装 / >500 bp / >1000 bp」之间切换查看指标。

## 软件文档

见 [references.md](references.md)。
