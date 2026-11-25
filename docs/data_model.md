# 数据模型

## 职责划分

| 存储 | 内容 |
| --- | --- |
| PostgreSQL | 分析步骤（`job_steps`）、与步骤调度相关的 `jobs` 行、可选的 samples/uploads/notices 副本 |
| SQLite `database/jobs.sqlite3` | Web 作业列表、上传、通知、样本等读模型；`TASK_BACKEND=postgres_rq` 时任务终态会写回此库 |
| `users.json` | 预配置账号 |
| 磁盘 `ASSEMBLY_DATA_ROOT` | FASTQ、结果、日志、归档；库中只存路径 |

Redis 只作 RQ 队列，不持久化业务状态。

## PostgreSQL

Schema：`migrations/*.sql`，应用：`python -m web.migrate_pg`。

主要表：

- `jobs`：id、owner、status、assemblers、threads、sample、SPAdes 选项、input/output/log 路径、取消与时间戳等
- `job_steps`：id、job_id、step_name、step_type（`fastp`|`assembly`|`quast`|`summary`|`noop`）、software、status、worker_type、container_id、attempt、日志与错误
- `samples`、`sequencing_batches`、`uploads`、`notices`

访问：`psycopg`（`web/pg.py`），无 ORM。

## SQLite（Web）

表定义见 `web/app.py` 中 `SCHEMA`（`jobs`、`job_steps`、`uploads`、`notices`、`samples`、`sequencing_batches`）。启动时用 `ensure_*` 做列/约束补齐。

## 从 SQLite 文件导入 Postgres

运维需要把现有 `jobs.sqlite3` 拷进 Postgres 时：

```bash
# 先备份，再导入（脚本会再写一份带时间戳的备份）
python -m web.migrate_sqlite_to_postgres --sqlite /path/to/jobs.sqlite3
```

要求已配置 `DATABASE_URL`，且已执行 `python -m web.migrate_pg`。
