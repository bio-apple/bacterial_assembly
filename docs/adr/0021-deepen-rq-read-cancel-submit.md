---
status: accepted
---

# 加深步骤读模型、任务入队、取消与运行时配置

Web 步骤展示只信 PostgreSQL（`web.step_read_model.load_recorded_steps`）；SQLite 步骤由 `sync_sqlite_steps_from_pg` 投影，不再用 `stages.json` 喂读模型（文件仍可写入供排障）。任务入队经 `submit_analysis_task`：先有 SQLite catalog，再 PG/RQ，失败统一补偿。取消经 `web.cancel.request_cancel`：`cancelling` → 停工具容器 → `cancelled`。`scripts.docker_executor` 通过注入的 CancelPoll / set_container，不再在模块加载时依赖 `web`。启动期 `web.runtime_config` 校验 `postgres_rq` 所需 URL；代码默认仍为 sqlite/local。legacy `web.worker` 保留但非推荐路径。
