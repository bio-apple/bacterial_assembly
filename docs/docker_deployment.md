# 部署

## 服务

| 服务 | 作用 |
| --- | --- |
| `web` | Flask / Gunicorn |
| `postgres` | 步骤与调度元数据 |
| `redis` | RQ 队列 |
| `worker-qc` | fastp、QUAST、summary |
| `worker-assembly` | SPAdes / Unicycler / Shovill / MEGAHIT |
| `worker-legacy` | 可选：`TASK_BACKEND=sqlite` 时单进程 `run_pipeline`（Compose profile `legacy-worker`） |

宿主机端口：Web `8000`，Postgres `5433`，Redis `6380`。

## 启动

```bash
# 仓库根目录：工具镜像
PLATFORM=linux/amd64 ./docker/tools/build.sh

cd deploy
cp .env.example .env   # 填写 SECRET、POSTGRES_PASSWORD、DOCKER_GID 等
mkdir -p data
sudo chown "$(id -u):$(id -g)" data

docker compose build
docker compose run --rm --entrypoint micromamba web \
  run -n base python -m web.migrate_pg
docker compose up -d
docker compose logs -f web worker-qc worker-assembly
```

默认 `TASK_BACKEND=postgres_rq`、`TOOL_RUNTIME=docker`。Worker 挂载 Docker socket；工具容器不挂 socket。

账号与 Nginx 等见 [web_deployment.md](web_deployment.md)。架构见 [architecture.md](architecture.md)。

## 本机连库冒烟

```bash
export DATABASE_URL=postgresql://assembly:CHANGE@127.0.0.1:5433/assembly
export REDIS_URL=redis://127.0.0.1:6380/0
PYTHONPATH=. python -m web.migrate_pg
PYTHONPATH=. python -m web.demo_queue --run-worker
# final_status=succeeded
```

## 代码入口

| 模块 | 路径 |
| --- | --- |
| 入队 | `web/job_submit.py` |
| 编排 | `web/orchestrator.py` |
| 步骤执行 | `web/step_runners.py` |
| RQ 任务 | `web/rq_jobs.py`、`web/rq_worker.py` |
| 取消 | `web/rq_cancel.py` |
| 发布 / 归档 | `web/job_publish.py` |
| 工具容器 | `scripts/docker_executor.py` |
