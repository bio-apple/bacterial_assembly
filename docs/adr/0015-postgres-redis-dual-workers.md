---
status: accepted
---

# PostgreSQL、Redis 与双 Worker

业务步骤状态持久化在 PostgreSQL；Redis 仅作 RQ 任务分发（组装队列与 QC 队列），不作唯一状态库。组装 Worker 执行四种组装；QC Worker 执行 fastp、QUAST 与 summary。生信软件经 DockerExecutor 与注册表白名单镜像运行。Web 读模型可同时使用 SQLite；从 SQLite 文件导入 Postgres 时须先备份。
