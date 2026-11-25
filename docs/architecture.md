# 系统架构

## 拓扑

```text
Flask / API
    |
    +---- SQLite（作业列表 / 上传等 Web 读模型）
    |
    +---- PostgreSQL（步骤状态）
    |
    +---- Redis / RQ
              |
     +--------+--------+
     |                 |
     v                 v
  组装队列            QC 队列
  worker-assembly     worker-qc
  SPAdes/…            fastp / QUAST / summary
     |                 |
     +--------+--------+
              |
              v
     ASSEMBLY_DATA_ROOT 共享卷
     + Docker Engine（生信工具容器）
```

- API 不执行生信软件，不等待容器结束。
- 组装 Worker 与 QC Worker 独立进程，共享同一数据根。
- Flask / Worker 在宿主机运行；生信软件仅在白名单工具镜像中运行（见 [docker_tools.md](docker_tools.md)、ADR 0020）。
- 工具镜像构建平台由宿主机判定，可被 `--platform` / `PLATFORM` 覆盖；支持 `linux/amd64` 与 `linux/arm64`（见 ADR 0019）。

## 分析流程

```text
上传并校验 FASTQ
  → QC：fastp（每个分析任务一次）
  → 组装：用户所选方法（默认全局串行；失败互不影响）
  → 每种成功组装：QC 上一次 QUAST 工作项（内含 original / gt500 / gt1000）
  → summary：comparison.tsv + assembly_outcome.json
  → 发布 output、可选归档
```

命令行入口：仓库根目录 `run_pipeline.py`（与 Web 共用 `scripts/pipeline_core` 中的 contig / QUAST 目标约定）。

## 工作项

| 类型 | 队列 | 入口 |
| --- | --- | --- |
| fastp | qc | `scripts/run_fastp.py` |
| assembly | assembly | `scripts/run_<method>.py` + `filter_contigs.py` |
| quast | qc | 同一工作项内三次 `run_quast.py` |
| summary | qc | `summarize_quast.py` + `assembly_outcome.json` |

| 职责 | 路径 |
| --- | --- |
| 任务入队 | `web/job_submit.submit_analysis_task` |
| 步骤图与依赖 | `web/orchestrator.py` |
| 步骤执行 | `web/step_runners.py` |
| RQ 入口 | `web/rq_jobs.py`、`web/rq_worker.py` |
| 任务取消 | `web/cancel.request_cancel` |
| 步骤读模型 | `web/step_read_model.py`（`postgres_rq` 读 PG） |
| SQLite 投影 | `web/sqlite_mirror.py` |
| 发布 / 归档 | `web/job_publish.py` |
| 路径解析 | `web/job_paths.py` |
| 工具容器 | `scripts/docker_executor.py` |
| 运行时配置 | `web/runtime_config.py` |
| SQLite schema | `web/db_sqlite.py` |
| 组装常量 | `scripts/pipeline_core.py`、`scripts/assembly_outcome.py` |

Redis 消息只带 `step_id` 等调度元数据；路径与参数以 PostgreSQL 与磁盘为准。

部署使用 `TASK_BACKEND=postgres_rq`；单测可使用 `TASK_BACKEND=sqlite`（`web/worker.py` 调 `run_pipeline.py`）。

## 状态

**分析任务**（Web / SQLite 读模型）：`queued` | `running` | `cancelling` | `cancelled` | `succeeded` | `partial_success` | `failed`

- fastp 失败 → `failed`
- 全部所选组装失败 → `failed`
- 至少一种组装成功且至少一种失败 → `partial_success`
- 全部组装成功 → `succeeded`（QUAST 失败不改变该终态；终态以 `assembly_outcome.json` + `finished_status` 裁决）
- 取消保留已完成产物
- `postgres_rq`：运行中与终态镜像到 SQLite；步骤展示读 PostgreSQL，并可投影到 SQLite

**工作项**（PostgreSQL `job_steps`）：`pending` | `queued` | `running` | `succeeded` | `failed` | `cancelled` | `skipped`

结果目录另有 `stages.json`、`assembly_outcome.json`。工具容器启动后写入 `container_id`；取消按任务标签停止容器；超时未更新的 `running` 可被回收为失败，以便基础设施类重试。

## 幂等与重试

- 以 `step_id` 领取；已成功则跳过；取消中的任务不启动。
- 自动重试仅限基础设施类错误（如 Docker Engine 暂不可用），默认最多 3 次。
- 工具非零退出、坏输入、用户取消不自动重试。

## 资源

`resource_budget` / `resource_limits`：约 90% CPU/内存写入工具参数；RQ Worker 与 CLI 下发 `_ASSEMBLY_ASSIGNED_THREADS`、`_ASSEMBLY_METHOD_MEMORY_GIB`。Docker 路径下容器内存上限为配额 + 1 GiB。组装队列默认并发 1。

## 数据根

`ASSEMBLY_DATA_ROOT`（宿主机绝对路径，EnvironmentFile 配置）：

```text
uploads/{chunks,incoming,validated}/
jobs/<id>/{input,work,output,logs}/
archive/
database/jobs.sqlite3
temp/
```

两 Worker 与工具容器均访问同一宿主机路径约定。
