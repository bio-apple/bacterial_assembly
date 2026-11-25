# 基于 NGS 的细菌基因组组装流程

该流程面向单个 Illumina 双端 FASTQ 样本，以 Docker 镜像运行 fastp 过滤与报告、用户选择的一种或多种组装方法、每份组装对应的 QUAST 无参考评估和指标汇总。fastp 与 QUAST 始终运行，至少选择一种组装方法；不依据生物学阈值判定样本失败，也不根据 N50 自动推荐最佳组装。

## 构建镜像

```bash
docker build --platform linux/amd64 -t bacterial-assembly:latest .
```

基础镜像是 `anaconda/miniconda:26.9.4`。镜像只安装软件环境，不包含流程和 Web 代码。arm64 宿主机也请按 `linux/amd64` 构建：SPAdes 3.15.5、QUAST 5.2.0 和 Shovill 依赖的 FLASH 只有 linux-64 包。

镜像内固定安装 fastp 0.23.4、SPAdes 3.15.5、Unicycler 0.5.1、Shovill 1.4.2、SKESA 2.5.1、MEGAHIT 1.2.9、QUAST 5.2.0、ValidateFastq 0.1.1、OpenJDK 8，以及 Flask/Gunicorn。Unicycler 与 SPAdes 4.0.0 装在独立的 `unicycler` 环境；其余工具在 base 环境。Compose 用 `micromamba run -n base` 启动 Web 与 worker。

## 运行

把仓库只读挂载到 `/opt/bacterial-assembly`，把当前目录挂载为容器工作目录，并将输入路径和结果路径写成相对该目录的路径：

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

输入支持 `.fastq`、`.fq` 及其 gzip 压缩文件。`--assembler` 可重复指定，取值为 `spades`、`unicycler`、`shovill`、`megahit`；至少指定一次，同一方法不可重复。所有选中的方法按 `--assembler` 顺序串行执行，共用同一份 fastp 过滤后 reads；每份组装均运行 QUAST。

逻辑 CPU 与物理内存先读取容器 cgroup（v1 或 v2）的有效上限；读不到时回退到主机。fastp 与每种组装方法共用同一配额：CPU 的 90%（向下取整，至少 1 个线程）和内存的 90%（按 GiB 向下取整，至少 1 GiB）。分配写入 `run.log`。SPAdes 传入 `-m`，Shovill 传入 `--ram`，MEGAHIT 传入等量字节的 `--memory`，Unicycler 通过 `--spades_options` 传入 `-m`。命令行、Web 表单和 API 均不接受手动线程数或内存上限。独立运行 QUAST 且未由总控分配线程时，使用全部逻辑 CPU。

`--spades-kmers` 与 `--spades-careful` 仅在选择 SPAdes 时生效；k-mer 为逗号分隔的奇数（最大 127），省略时用 SPAdes 默认值。Web worker 还会传入可选的 `--job-id`；命令行省略该参数时仍可独立运行。

某个组装方法失败不取消其余方法。全部成功时退出码 0；部分成功时退出码 3，并写出 `assembly_outcome.json`；全部失败或 fastp 等前置步骤失败时退出码 1。Worker 根据该文件中的状态、退出码和对应 contig 文件判定任务结果，不能只凭 `comparison.tsv` 是否存在。

## 独立软件脚本

总控 `run_pipeline.py` 先运行 fastp，再按 `--assembler` 顺序调用所选组装方法及其 QUAST，最后只汇总本次成功的原始组装。需要单独运行某个步骤时：

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

各软件脚本通过 `subprocess` 调用工具；可用 `--log` 和 `--versions-file` 指定日志与版本文件。省略时，这些文件写在该步骤结果目录的上级目录。

## 结果结构

```text
results/isolate_01/
├── assemblies/
│   ├── megahit/
│   │   ├── final.contigs.fa
│   │   ├── contigs_gt500bp.fasta
│   │   └── contigs_gt1000bp.fasta
│   ├── shovill/
│   ├── spades/
│   └── unicycler/
├── assembly_outcome.json        # 组装结果声明
├── comparison.tsv               # 仅成功原始组装的 QUAST 指标横向汇总
├── fastp/
│   ├── isolate_01_R1.fastq.gz
│   ├── isolate_01_R2.fastq.gz
│   ├── isolate_01_fastp.html
│   └── isolate_01_fastp.json
├── quast/
│   └── <assembler>/
│       ├── report.tsv
│       ├── gt500bp/report.tsv
│       └── gt1000bp/report.tsv
├── run.log
└── versions.txt
```

每种被选中的组装都输出原始 FASTA 和两个长度过滤 FASTA（严格大于 500 bp / 1000 bp）。QUAST 分别评估原始组装和非空筛选结果。`comparison.tsv` 只汇总本次成功的原始组装。`assembly_outcome.json` 至少包含协议版本、状态、所选/成功/失败的组装方法和退出码，先写临时文件再原子替换。结果目录必须不存在或为空。

## 工具调用依据

- [Unicycler Illumina-only 用法](https://github.com/rrwick/Unicycler#quick-usage)：只用 `-1/-2`；`-l` 是长读长输入。
- [Shovill 参数](https://github.com/tseemann/shovill#advanced-options)：双端参数 `--R1/--R2`、资源参数 `--cpus/--ram` 和 `--assembler skesa`。
- [MEGAHIT 用法](https://github.com/voutcn/megahit#usage)：双端 FASTQ 使用 `-1/-2`，输出目录使用 `-o`。
- [QUAST 用法](https://github.com/ablab/quast#usage)：支持无参考评估，`report.tsv` 是可解析的指标表。

## 本地参数检查与流程冒烟测试

```bash
python3 -m pip install -r requirements-web.txt
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh
```

## Linux 浏览器工作台部署

Web 工作台定位为「微生物 NGS 数据分析系统」。用户登录后进入 `/overview`，查看本人任务与本机资源占用，再从 `/modules` 进入细菌基因组组装模块。

### 上传与校验

浏览器按 8 MiB 分片上传 R1/R2。分片到齐后：

1. **字节校验**：服务器计算 SHA-256，与浏览器提交值一致才继续。
2. **记录校验**：`.gz` 先做完整 gzip 读取；再用 ValidateFastq 0.1.1 检查记录（退出码 0，且输出含 `no errors found`，不含 `Error found at readnumber`）。
3. 创建任务前再核对 R1/R2 记录数量与 read 编号配对。

任一步失败时文件不可分析，不能启动组装。校验日志写在临时目录或任务 `logs/`。通过后两份文件才可提交分析。

### 分析与状态

提交后选择样本名和至少一种组装方法。fastp 先运行一次，所选方法按选择顺序串行运行并使用同一资源配额。任务进入服务器端 FIFO 串行队列。

任务状态：排队中、分析中、已完成、部分完成、失败、取消中、已取消。

- 全部组装方法成功：`succeeded`
- 部分组装方法成功：`partial_success`（可下载部分结果）
- 全部组装方法失败，或 fastp 等前置步骤失败：`failed`
- 用户取消：`cancelled`

`job_steps` 记录每个步骤的状态，与任务终态一致。排队中的步骤在终态任务上记为 skipped；部分完成时仍在运行的步骤记为 failed。不提供单步重跑。

任务详情可在「原始组装 / >500 bp / >1000 bp」之间切换查看指标，不按 N50 排序或标出最优。fastp 的 HTML 报告可在新窗口打开，页面另有质控指标摘要。

提交者可取消自己尚未结束的任务。已完成、部分完成、失败或已取消的任务不能再取消。取消后保留已有输出和日志，不为取消临时打包。

`/history` 按账号隔离显示任务；可下载结果或日志，并可重新分析（新建任务，不覆盖原任务）。

### 分析模块扩展

`/modules` 与工作台导航由 `web/modules.py` 中的 `ANALYSIS_MODULES` 驱动。新增模块时先实现登录保护路由和页面，再登记模块 ID。只有路由和页面准备好后才应登记。新页面沿用顶栏和 `web/templates/components/` 中的上传区、状态标签、运行日志和确认框。

### 配置账号与密钥

在 Linux 服务器安装 Docker Engine、Docker Compose plugin、Nginx，并将仓库放到服务器：

```bash
cd deploy
cp .env.example .env
cp users.json.example users.json
chmod 600 .env users.json
```

在 `.env` 中设置 `ASSEMBLY_WEB_SECRET`、`APP_UID`、`APP_GID`（密钥无默认值）。生成密码哈希：

```bash
docker compose build
docker compose run --rm --entrypoint micromamba web \
  run -n base python /opt/bacterial-assembly/web/password_hash.py
```

把账号与 scrypt 哈希填入 `users.json`。然后：

```bash
mkdir -p data
sudo chown "$(id -u):$(id -g)" data
docker compose up -d --build
docker compose logs -f web worker
```

Compose 将仓库只读挂载到 `/opt/bacterial-assembly`。修改流程或 Web 代码后重启服务即可，不必为此重建镜像。Web 端口绑定 `127.0.0.1:8000`。运行数据根目录是 `deploy/data/`（容器内 `/data`，`ASSEMBLY_DATA_ROOT`）：

```text
data/
  uploads/incoming/
  uploads/chunks/
  uploads/validated/
  jobs/<任务编号>/input/
  jobs/<任务编号>/work/
  jobs/<任务编号>/output/
  jobs/<任务编号>/logs/
  reports/
  database/jobs.sqlite3
  temp/
  archive/
```

分析只读 `jobs/<任务编号>/input`。若数据根下仍有旧的 `jobs.sqlite3` 而 `database/jobs.sqlite3` 不存在，启动时会把旧库移过去，不删表。worker 使用数据根上的 `worker.lock`；不要横向扩容 worker。

### HTTPS 反向代理

将 `deploy/nginx.conf` 安装到 Nginx `conf.d`，替换证书路径与服务器名后重载。防火墙只开放 HTTPS；不要把 8000 端口暴露到公网。登录使用安全、HttpOnly、SameSite=Lax 的 HTTPS cookie，并验证 CSRF；应用信任一跳 `X-Forwarded-*` 头。

### 结果保留与手动清理

上传 FASTQ、作业数据库、日志、分析结果和下载压缩包一直保留到管理员清理；Web 界面不提供删除。完全清理前先停止服务，再手动归档或删除 `deploy/data/` 中的数据。
