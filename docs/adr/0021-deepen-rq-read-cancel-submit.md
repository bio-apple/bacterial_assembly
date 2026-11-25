---
status: accepted
---

# 步骤读模型、任务入队、取消与运行时配置

Web 步骤展示以 PostgreSQL 为真源（`web.step_read_model.load_recorded_steps`）；SQLite 步骤由 `sync_sqlite_steps_from_pg` 投影。`stages.json` 仍可写入供排障，不作 Web 读模型真源。任务入队经 `submit_analysis_task`：先写 SQLite catalog，再写 PG 并入队，失败统一补偿。取消经 `web.cancel.request_cancel`：`cancelling` → 停工具容器 → `cancelled`。`scripts.docker_executor` 经注入的 CancelPoll / set_container 与 Web 协作。启动期 `web.runtime_config` 在 `postgres_rq` 下校验 `DATABASE_URL` / `REDIS_URL`。代码未设置时 `TASK_BACKEND` 默认为 `sqlite`、`TOOL_RUNTIME` 默认为 `local`；生产 EnvironmentFile 设为 `postgres_rq` 与 `docker`。
