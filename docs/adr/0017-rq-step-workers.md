---
status: accepted
---

# RQ 步骤 Worker

任务分发用 RQ：组装队列与 QC 队列分列。创建与重新分析经 `web/job_submit` 写入 Postgres 步骤并投递首个工作项。Worker 按工作项调用 `scripts/run_*.py`（及 contig 过滤、汇总脚本），工具执行走 DockerExecutor。`assembly_outcome.json` 写在结果目录；步骤状态在 PostgreSQL。Postgres 用 psycopg 与版本化 SQL schema。Web、双 Worker 与工具容器共享同一 `ASSEMBLY_DATA_ROOT`。
