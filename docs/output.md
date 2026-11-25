# 结果文件与指标

## 目录结构

结果目录必须不存在或为空。仅所选且已启动的方法会在 `assemblies/` 下出现目录。

```text
results/isolate_01/
├── assemblies/
│   └── <assembler>/
│       ├── <contigs>              # 方法成功时的原始组装 FASTA
│       ├── contigs_gt500bp.fasta
│       └── contigs_gt1000bp.fasta
├── assembly_outcome.json
├── comparison.tsv                 # 仅成功原始组装；无成功方法时不写
├── fastp/
│   ├── <sample>_R1.fastq.gz
│   ├── <sample>_R2.fastq.gz
│   ├── <sample>_fastp.html
│   └── <sample>_fastp.json
├── quast/
│   └── <assembler>/               # 仅成功方法；空筛选目录可能不存在
│       ├── report.tsv
│       ├── gt500bp/report.tsv
│       └── gt1000bp/report.tsv
├── stages.json
├── run.log
└── versions.txt
```

带 `--job-id` 时还会写出 `process.json`。

## 组装结果声明

`assembly_outcome.json` 字段：

- `schema_version`
- `status`：`succeeded` / `partial_success` / `failed`
- `selected_assemblers`
- `successful_assemblers`
- `failed_assemblers`
- `exit_code`

Worker 核对声明、进程退出码，以及每个成功方法的非空 contig；不能只凭 `comparison.tsv` 是否存在判定成功。

## 比较表与口径

`comparison.tsv` 只汇总本次成功的原始组装 QUAST 指标。长度筛选结果的报告在 `quast/<assembler>/gt500bp/` 与 `gt1000bp/`，不与原始口径混在同一张导出表里。不要把同一口径下更大的数值当作更优组装。

## 部分完成时可用结果

任务为 `partial_success` 时可下载归档中已有内容：成功方法的 contig、对应 QUAST，以及仅含成功方法的 `comparison.tsv`（若有）。
