# 基于 NGS 的细菌基因组组装流程

面向单个 Illumina 双端 FASTQ 样本：fastp 过滤与报告、一种或多种组装方法、成功组装上的 QUAST 无参考评估与指标汇总。至少选择一种组装方法；不依据生物学阈值判定样本失败，也不根据 N50 自动推荐最佳组装。

## 核心能力

- Web：Flask + Postgres 步骤调度 + Redis/RQ（组装 Worker / QC Worker）；生信软件在工具侧车容器中运行（[docs/architecture.md](docs/architecture.md)、[docs/docker_tools.md](docs/docker_tools.md)）
- 可选 SPAdes、Unicycler（Illumina-only）、Shovill/SKESA、MEGAHIT；默认组装串行，失败互不影响
- 资源：工具参数约 90% 配额；容器另有 CPU/内存上限（内存为配额 + 1 GiB）
- CLI（`run_pipeline.py`）与 Web；分片上传、校验、取消、部分完成与重新分析

## 分析流程

```text
双端 FASTQ
  → fastp（过滤 + HTML/JSON）
  → 按选择顺序串行组装（失败不取消其余方法）
  → 成功方法：长度筛选（>500 / >1000 bp）+ QUAST
  → comparison.tsv（仅成功原始组装）+ assembly_outcome.json
```

CLI 退出码：全部成功 `0`，部分成功 `3`，全部失败或前置失败 `1`。Web 将部分成功记为 `partial_success`（部分完成），不会仅因非零退出码记为失败。

## 支持的软件与版本

生信工具在侧车镜像中运行（见 [docs/docker_tools.md](docs/docker_tools.md)）：

| 软件 | 版本 |
| --- | --- |
| fastp | 0.23.4 |
| SPAdes | 3.15.5 |
| Unicycler | 0.5.1（镜像内含 SPAdes 4.0.0） |
| Shovill / SKESA | 1.4.2 / 2.5.1 |
| MEGAHIT | 1.2.9 |
| QUAST | 5.2.0 |

应用镜像含 ValidateFastq 0.1.1、OpenJDK 8、Flask 3.1.1、Gunicorn 23.0.0。完整说明见 [docs/installation.md](docs/installation.md)。

## 安装与快速开始

```bash
# 构建平台默认跟宿主机；可 --platform / PLATFORM 覆盖（ADR 0019）
./docker/build-app.sh

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
  --assembler megahit
```

参数、资源与独立脚本见 [docs/usage.md](docs/usage.md)。部署见 [docs/docker_deployment.md](docs/docker_deployment.md) 与 [docs/web_deployment.md](docs/web_deployment.md)。

## 输入文件要求

- 成对的 R1/R2：`.fastq`、`.fq` 或其 gzip
- 至少一种组装方法；`--spades-kmers`（奇数、≤127）与 `--spades-careful` 仅在选择 SPAdes 时有效
- Web：分片上传后需通过字节校验、记录校验；创建任务前再做 R1/R2 配对检查

## 结果文件说明

结果目录须为空。成功方法写出原始 contig 与 `contigs_gt500bp.fasta` / `contigs_gt1000bp.fasta`；另有 `fastp/`、`quast/`、`comparison.tsv`（若有成功方法）、`assembly_outcome.json`、`stages.json`、`run.log`、`versions.txt`。

字段与口径说明见 [docs/output.md](docs/output.md)。

## 注意事项

- 构建平台：`--platform` > `PLATFORM` > 宿主机判定；支持 `linux/amd64` 与 `linux/arm64`（ADR 0019）
- 不要把宿主机物理内存直接当成容器可用内存；以 cgroup 有效上限为准
- 命令行与 Web 均不提供手动线程数/内存参数
- 部分组装方法失败时其余方法继续；Web 为部分完成，CLI 退出码为 3
- 不根据 N50 推荐「最佳」组装

## 引用与许可

软件文档链接见 [docs/references.md](docs/references.md)。仓库根目录当前未附带 `LICENSE` 文件；许可条款以维护者声明为准（无法从代码自动确认）。

## 详细文档

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 系统架构与调度 |
| [docs/data_model.md](docs/data_model.md) | 数据存储模型 |
| [docs/docker_deployment.md](docs/docker_deployment.md) | Compose 部署 |
| [docs/docker_tools.md](docs/docker_tools.md) | 生信工具镜像 |
| [docs/web_deployment.md](docs/web_deployment.md) | Web 账号与任务状态 |
| [docs/developer_guide.md](docs/developer_guide.md) | 开发扩展 |
| [docs/installation.md](docs/installation.md) | 镜像构建与环境 |
| [docs/usage.md](docs/usage.md) | 命令行用法与资源 |
| [docs/assembly_methods.md](docs/assembly_methods.md) | 组装方法与参数 |
| [docs/output.md](docs/output.md) | 结果与指标 |
| [docs/security.md](docs/security.md) | 安全与数据清理 |
| [docs/references.md](docs/references.md) | 软件文档链接 |
| [CONTEXT.md](CONTEXT.md) | 领域术语 |
| [docs/adr/](docs/adr/) | 架构决策 |
