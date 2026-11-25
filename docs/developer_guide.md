# 开发者指南

## `TASK_BACKEND`

| 值 | 行为 |
| --- | --- |
| `sqlite` | 创建 / 重新分析只写 SQLite；由 `web.worker` 调 `run_pipeline.py`（单测默认） |
| `postgres_rq` | 写 SQLite 读模型，并经 `web.job_submit.enqueue_rq_pipeline` 在 Postgres 建步骤、RQ 执行（推荐部署默认） |

## 关键模块

| 模块 | 作用 |
| --- | --- |
| `web/job_submit.py` | 创建与重新分析共用的 RQ 入队 |
| `web/orchestrator.py` | 步骤依赖、终态、发布触发 |
| `web/step_runners.py` | 按步调用 `scripts/run_*.py` |
| `web/rq_jobs.py` | RQ 领取、资源环境变量、取消探测 |
| `web/sqlite_mirror.py` | 任务状态 / 步骤同步到 SQLite |
| `web/step_read_model.py` | 任务页步骤列表的 Postgres 适配 |
| `web/job_publish.py` | work→output 发布与 tar 归档 |
| `web/job_paths.py` | work / stages / outcome 路径 |
| `scripts/pipeline_core.py` | 组装方法名、contig 文件名、QUAST 目标 |
| `scripts/docker_executor.py` | 白名单工具容器 |
| `config/software_registry.yaml` | 镜像与可执行文件（含 Unicycler bundled SPAdes 标签） |

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

架构：[architecture.md](architecture.md)。部署：[host_deployment.md](host_deployment.md)。
