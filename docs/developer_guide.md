# 开发者指南

## `TASK_BACKEND`

| 值 | 行为 |
| --- | --- |
| `sqlite` | Web 创建任务后由 `web.worker` 调 `run_pipeline.py`（单测默认） |
| `postgres_rq` | 创建任务写入 SQLite 读模型，并在 Postgres 建步骤、经 RQ 执行（Compose 默认） |

## 扩展步骤类型

1. 如需新 `step_type`，改 `migrations/` 并应用  
2. 在 `web/step_runners.py` 实现执行  
3. 在 `web/rq_jobs.py` 分支调用  
4. 在 `web/orchestrator.py` 定义依赖与入队  

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests
bash tests/smoke.sh
```

架构：[architecture.md](architecture.md)。部署：[docker_deployment.md](docker_deployment.md)。
