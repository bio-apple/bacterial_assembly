# 基于 NGS 的细菌基因组组装流程

该流程面向单个 Illumina 双端 FASTQ 样本，以 Docker 镜像运行 fastp 过滤与报告、用户选择的一种或多种组装器、每份组装对应的 QUAST 无参考评估和指标汇总。fastp 与 QUAST 始终运行，至少选择一种组装器；不依据生物学阈值判定样本失败。

## 构建镜像

```bash
docker build -t bacterial-assembly:latest .
```

镜像只安装软件环境，不包含流程和 Web 代码。其中固定使用 fastp 0.23.4、SPAdes 3.15.5、Unicycler 0.5.1、Shovill 1.4.2、SKESA 2.5.1、MEGAHIT 1.2.9、QUAST 5.2.0，以及 Flask/Gunicorn。由于 Bioconda 的 Unicycler 配方要求 SPAdes 4.0.0 以上，而 Shovill 配方要求 SPAdes 4 以下，镜像在独立环境中安装 Unicycler/SPAdes 4.0.0；其余流程使用 SPAdes 3.15.5。

## 运行

把仓库只读挂载到 `/opt/bacterial-assembly`，把当前目录挂载为容器工作目录，并将输入路径和结果路径写成相对该目录的路径：

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
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

输入支持 `.fastq`、`.fq` 及其 gzip 压缩文件。`--assembler` 可重复指定，取值为 `spades`、`unicycler`、`shovill`、`megahit`；至少指定一次，重复选择同一组装器会报错。所有选中的组装方法按 `--assembler` 的顺序串行执行，并使用同一份 fastp 过滤后 reads；每份组装均运行 QUAST。每种方法尝试使用逻辑 CPU 数的 90%（向下取整，至少 1 个线程）和物理内存的 90%（按 GiB 向下取整，至少 1 GiB）：SPAdes 传入 `-m`，Shovill 传入 `--ram`，MEGAHIT 传入等量字节的 `--memory`，Unicycler 通过 `--spades_options` 传入 `-m`。Unicycler 0.5.1 在该参数含 `-m` 时不再追加内部写死的 `-m 1024`。fastp 在组装前独立运行，使用全部逻辑 CPU。Web 样本任务仍按 FIFO 串行执行。组装方法互相隔离：某个方法失败不取消其他方法；若至少一种方法成功，仍会生成成功方法的比较汇总，CLI 以非零状态结束，Web 任务标记失败并提供部分结果下载。命令行、Web 表单和 API 均不接受手动线程数或内存上限。Unicycler 还会将内部 SPAdes 线程数限制为最多 32；Shovill 上游提示其底层 SPAdes 对超过 16 线程的支持可能不稳定，调用仍按 90% 线程传递。工具默认值和行为可能随版本变化；实际可用资源也受操作系统、容器和调度器限制。`--spades-kmers` 与 `--spades-careful` 仅控制 SPAdes；k-mer 为逗号分隔的奇数列表（最大 127），省略时使用 SPAdes 默认值。运行 `docker run --rm -v /path/to/bacterial_assembly:/opt/bacterial-assembly:ro bacterial-assembly:latest python3 /opt/bacterial-assembly/run_pipeline.py --help` 查看参数。

fastp 每次运行一次，产生过滤后双端 reads 和 HTML/JSON 报告。所选组装器按顺序读取同一对过滤后 reads；Unicycler 只传入 `-1/-2`，不传长读长参数 `-l`，因此运行 Illumina-only short-read workflow，而不是 hybrid assembly。Shovill 显式选择 SKESA 作为组装引擎。每份组装完成后立即以无参考模式运行 QUAST。

每份原始组装还会生成 `contigs_gt500bp.fasta` 和 `contigs_gt1000bp.fasta`，分别只保留长度严格大于 500 bp 和 1000 bp 的 contig。QUAST 依次评估原始组装、非空的 >500 bp 结果和非空的 >1000 bp 结果；空筛选文件仍保留，并在日志中记录跳过原因。阈值结果报告单独放在各组装器的 `quast/<assembler>/gt500bp/` 与 `gt1000bp/` 中，不混入默认的 `comparison.tsv`；该汇总只比较所选组装器的原始组装。

## 独立软件脚本

总控 `run_pipeline.py` 先运行 fastp，再按 `--assembler` 的顺序调用所选组装器及其 QUAST，最后只汇总本次选中的组装器；该顺序同时是执行顺序和比较表中的排列。需要单独运行某个步骤时，可先查看对应参数：

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

各软件脚本都直接通过 `subprocess` 调用工具；可用 `--log` 和 `--versions-file` 指定日志与版本文件。省略时，这些文件写在该步骤结果目录的上级目录。独立运行前需准备相应工具及输入；例如 Unicycler 需在含其运行依赖的环境中执行。独立汇总时需按报告选择传入组装器，例如 `python3 scripts/summarize_quast.py --outdir results/isolate_01 --assembler spades --assembler megahit`。

## 结果结构

```text
results/isolate_01/                 # 显示全部四种组装器；仅所选项会生成
├── assemblies/
│   ├── megahit/
│   │   ├── final.contigs.fa
│   │   ├── contigs_gt500bp.fasta
│   │   └── contigs_gt1000bp.fasta
│   ├── shovill/
│   ├── spades/
│   └── unicycler/
├── comparison.tsv               # QUAST 指标横向汇总
├── fastp/
│   ├── isolate_01_R1.fastq.gz
│   ├── isolate_01_R2.fastq.gz
│   ├── isolate_01_fastp.html
│   └── isolate_01_fastp.json
├── quast/
│   └── <assembler>/
│       ├── report.tsv            # 原始组装
│       ├── gt500bp/report.tsv    # 非空时
│       └── gt1000bp/report.tsv   # 非空时
├── run.log
└── versions.txt
```

每种被选中的组装都输出原始 FASTA 和两个长度过滤 FASTA；阈值比较严格使用 `>`。QUAST 分别评估原始组装和非空筛选结果，空筛选 FASTA 保留并记录跳过信息。`comparison.tsv` 只汇总本次选择的原始组装，摘录 contig 数、总长度、最大 contig、N50、L50 和 GC 百分比。未选中的组装器不会产生结果目录。结果目录必须不存在或为空，以免覆盖先前结果；重新运行时请指定新目录。`run.log` 记录运行步骤、过滤结果与工具输出；`versions.txt` 记录本次实际运行的工具版本，包括 Unicycler 专用环境中的 SPAdes 版本。

## 工具调用依据

- [Unicycler Illumina-only 用法](https://github.com/rrwick/Unicycler#quick-usage)：只用 `-1/-2`；`-l` 是长读长输入。
- [Shovill 参数](https://github.com/tseemann/shovill#advanced-options)：双端参数 `--R1/--R2`、资源参数 `--cpus/--ram` 和 `--assembler skesa`。
- [MEGAHIT 用法](https://github.com/voutcn/megahit#usage)：双端 FASTQ 使用 `-1/-2`，输出目录使用 `-o`。
- [QUAST 用法](https://github.com/ablab/quast#usage)：支持无参考评估，`report.tsv` 是可解析的指标表。

## 本地参数检查与流程冒烟测试

安装 Web 测试依赖后运行语法、模拟 CLI 和 Web 鉴权/队列测试：

```bash
python3 -m pip install -r requirements-web.txt
bash tests/smoke.sh
```

## Linux 浏览器工作台部署

Web 工作台定位为“微生物 NGS 数据分析系统”。用户登录后进入 `/modules` 模块目录，查看管理员已启用的分析模块并点击卡片进入相应工作台；当前提供细菌基因组组装模块。进入后上传 R1/R2，选择样本名和至少一个组装器，再手动提交。fastp 先运行一次，所选组装方法按选择顺序串行运行并使用同一份过滤后 reads；每种方法尝试使用约 90% 的逻辑 CPU 和物理内存。每份原始和非空的长度筛选组装均由 QUAST 评估。样本任务进入服务器端 FIFO 串行队列，页面轮询显示任务状态和实时日志；完成后可通过浏览器下载全部输出，部分方法失败时可下载成功方法的部分结果。`/history` 处理历史页按样本显示分析模块、所选组装器、任务状态、结束时间和运行时间；每条可用记录可直接下载输出结果，并可下载运行日志。运行中任务的耗时会实时更新，时间按浏览器时区显示，历史记录按账号隔离并分页。任务、日志和结果只对提交该任务的账号开放；不提供注册、取消或删除功能。

### 分析模块扩展

`/modules` 模块目录与工作台模块导航由 `web/modules.py` 中的 `ANALYSIS_MODULES` 驱动。新增分析模块时，先实现独立的登录保护路由和页面，再登记模块 ID、名称、标识、说明和 Flask endpoint；模块目录会自动生成入口卡片，工作台导航会高亮当前模块。新模块提交任务时还需把对应 ID 写入作业记录，以便处理历史正确显示模块归属。只有路由和分析页面准备好后才应登记；现有组装流程保持不变。

### 配置账号与密钥

在 Linux 服务器安装 Docker Engine、Docker Compose plugin、Nginx，并将仓库放到服务器。进入部署目录，准备私密配置：

```bash
cd deploy
cp .env.example .env
cp users.json.example users.json
chmod 600 .env users.json
```

在 `.env` 中设置随机密钥和运行容器的 Linux UID/GID：

```bash
openssl rand -hex 32
id -u
id -g
```

将命令输出分别填入 `ASSEMBLY_WEB_SECRET`、`APP_UID` 和 `APP_GID`。密钥没有默认值；未设置时服务拒绝启动。创建管理员预配账号的密码哈希：

```bash
docker compose build
docker compose run --rm --entrypoint micromamba web \
  run -n base python /opt/bacterial-assembly/web/password_hash.py
```

把输出的账号与哈希填入 `users.json`，格式为 `{"users":{"labuser":"scrypt:..."}}`。密码以 scrypt 哈希保存，不写入明文配置；账号只能由服务器管理员添加或移除。之后准备持久数据目录并启动：

```bash
mkdir -p data
sudo chown "$(id -u):$(id -g)" data
docker compose up -d --build
docker compose logs -f web worker
```

Compose 将仓库只读挂载到 `/opt/bacterial-assembly`，因此修改流程或 Web 代码后重启服务即可，不必为此重新构建镜像。运行容器的账号必须能读取这份仓库。Web 端口仅绑定到 `127.0.0.1:8000`，输入、SQLite 队列、日志和结果保存在 `deploy/data/`。请把该目录放在有足够空间的持久磁盘上；Web 进程与单一 worker 共享该目录。worker 使用文件锁防止第二个 worker 并发消费；不要横向扩容 worker 服务。

### HTTPS 反向代理

`deploy/nginx.conf` 是 Nginx HTTPS 配置示例。将其安装到 Nginx `conf.d`，把证书路径替换为服务器已有的 TLS 证书，并将服务器名按需配置：

```bash
sudo cp nginx.conf /etc/nginx/conf.d/assembly.conf
sudo nginx -t
sudo systemctl reload nginx
```

证书必须与访问地址匹配；若直接用 IP 访问，证书需包含该 IP 地址的 SAN，否则浏览器会显示证书警告。防火墙只开放 HTTPS（需要 HTTP 跳转时再开放 80）；不要将应用的 8000 端口暴露到公网。示例 Nginx 将上传体积上限设为不限制，并关闭请求缓冲；上传实际受反向代理配置、`deploy/data/` 所在磁盘容量和临时空间约束。应用不设置 FASTQ 大小阈值，磁盘满时会报告上传失败。

登录使用安全、HttpOnly、SameSite=Lax 的 HTTPS cookie，并验证 CSRF token；Nginx 示例对登录路径限速。只应通过可信的本机反向代理访问 Gunicorn，因为应用信任一跳 `X-Forwarded-*` 头。

### 结果保留与手动清理

上传 FASTQ、作业数据库、日志、分析结果和下载压缩包都会一直保留，直到管理员清理；Web 界面不提供删除入口。定期监控磁盘空间并先备份需要保留的结果。完全清理前先停止服务，再由管理员手动归档或删除 `deploy/data/` 中的数据；删除队列数据库会一并移除作业历史。完成后重新启动服务。
