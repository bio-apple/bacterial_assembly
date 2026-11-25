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
- 生信软件在白名单工具镜像中运行（见 [docker_tools.md](docker_tools.md)）。

## 分析流程

```text
上传并校验 FASTQ
  → QC：fastp（每个分析任务一次）
  → 组装：用户所选方法（默认全局串行；失败互不影响）
  → 每种成功组装：QC 上一次 QUAST 工作项（内含 original / gt500 / gt1000）
  → summary：comparison.tsv + assembly_outcome.json
  → 发布 output、可选归档
```

CLI 仍可用仓库根目录 `run_pipeline.py` 在本机一次性跑通同一科学流程。

## 工作项

| 类型 | 队列 | 入口 |
| --- | --- | --- |
| fastp | qc | `scripts/run_fastp.py` |
| assembly | assembly | `scripts/run_<method>.py` + `filter_contigs.py` |
| quast | qc | 同一工作项内三次 `run_quast.py` |
| summary | qc | `summarize_quast.py` + `assembly_outcome.json` |

编排与入队：`web/orchestrator.py`。执行：`web/step_runners.py`。RQ 入口：`web/rq_jobs.py`。

Redis 消息只带 `step_id` 等调度元数据；路径与参数以 PostgreSQL 与磁盘为准。

## 状态

**分析任务**（Web / SQLite 读模型）：`queued` | `running` | `cancelling` | `cancelled` | `succeeded` | `partial_success` | `failed`

- fastp 失败 → `failed`
- 全部所选组装失败 → `failed`
- 至少一种组装成功且至少一种失败 → `partial_success`
- 全部组装成功 → `succeeded`（QUAST 失败不改变该终态；终态以 `assembly_outcome.json` + `finished_status` 裁决）
- 取消保留已完成产物
- RQ 路径会把运行中/终态镜像回 SQLite；步骤表优先来自 `stages.json` 同步，空表时由 PostgreSQL 适配器补齐

**工作项**（PostgreSQL `job_steps`）：`pending` | `queued` | `running` | `succeeded` | `failed` | `cancelled` | `skipped`

结果目录另有 `stages.json`、`assembly_outcome.json`（组装成败列表）。工具容器启动后写入 `container_id`；取消按任务标签停止容器；超时未心跳的 `running` 可被回收为失败以便基础设施重试。

## 幂等与重试

- 以 `step_id` 领取；已成功则跳过；取消中的任务不启动。
- 自动重试仅限基础设施类错误（如 Docker Engine 暂不可用），默认最多 3 次。
- 工具非零退出、坏输入、用户取消不自动重试。

## 资源

`resource_budget` / `resource_limits`：约 90% CPU/内存写入工具参数；Docker 路径下容器内存上限为配额 + 1 GiB。组装队列默认并发 1。

## 数据根

`ASSEMBLY_DATA_ROOT`（Compose：`/data` ← `deploy/data/`）：

```text
uploads/{chunks,incoming,validated}/
jobs/<id>/{input,work,output,logs}/
archive/
database/jobs.sqlite3
temp/
```

两 Worker 与工具容器均访问同一宿主机路径约定。
