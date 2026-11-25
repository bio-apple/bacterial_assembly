---
status: accepted
---

# RQ 按步骤调度

任务分发用 RQ：组装队列与 QC 队列分列。Worker 按工作项调用 `scripts/run_*.py`（及 contig 过滤、汇总脚本），工具执行走 DockerExecutor。`assembly_outcome.json` 写在结果目录；步骤状态在 PostgreSQL。Postgres 用 psycopg 与版本化 SQL 迁移。Compose：API、Postgres、Redis、组装 Worker、QC Worker 共享同一 `ASSEMBLY_DATA_ROOT`。
