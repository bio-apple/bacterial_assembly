# 宿主机部署（推荐）

Flask / Gunicorn / RQ Worker 跑在 Linux 宿主机；PostgreSQL 与 Redis 用系统包或外部实例；仅六个生信工具使用 Docker 镜像（ADR 0020）。

## 系统要求

- Linux（x86_64 或 aarch64）
- Python ≥ 3.10
- Docker Engine（构建与运行工具镜像）
- PostgreSQL 16+（或兼容）
- Redis 7+（或兼容）
- Nginx（对外 HTTPS）
- 可选：micromamba（安装 ValidateFastq / OpenJDK）

## 1. 安装系统依赖

发行版示例（或使用 `./deploy/install_host.sh --install-deps`）：

```bash
# Debian/Ubuntu 示意
sudo apt-get update
sudo apt-get install -y python3 python3-venv postgresql redis-server nginx
# 另按官方文档安装 Docker Engine
```

创建数据库角色与库（名称须与 `DATABASE_URL` 一致），并启用 Redis。

## 2. 构建生信工具镜像

```bash
cd /path/to/bacterial_assembly
./docker/tools/build.sh
docker images | grep bacterial-assembly
```

平台解析见 ADR 0019（`--platform` > `PLATFORM` > 宿主机）。

## 3. Python 虚拟环境

```bash
python3 -m venv /opt/bacterial-assembly/.venv
/opt/bacterial-assembly/.venv/bin/pip install -r requirements-host.txt
```

`requirements-host.txt` 与 `requirements-web.txt` 钉同一组 Web/Worker 依赖；不在宿主机安装 SPAdes/fastp/QUAST。

## 4. ValidateFastq（micromamba）

```bash
# 示例：仓库旁 micromamba 根
export MAMBA_ROOT_PREFIX=/opt/bacterial-assembly/.mamba
micromamba create -y -p /opt/bacterial-assembly/.mamba/envs/validate \
  -c conda-forge -c bioconda \
  python=3.10 biopet-validatefastq=0.1.1 openjdk=8
```

systemd 单元把该 env 的 `bin` 放在 `PATH` 前面。

## 5. 环境变量

```bash
sudo mkdir -p /etc/bacterial-assembly
sudo cp deploy/assembly.env.example /etc/bacterial-assembly/assembly.env
sudo chmod 600 /etc/bacterial-assembly/assembly.env
# 编辑：ASSEMBLY_WEB_SECRET、DATABASE_URL、REDIS_URL、ASSEMBLY_DATA_ROOT 等
```

沿用代码中的变量名（`ASSEMBLY_WEB_SECRET`、`ASSEMBLY_DATA_ROOT`、`TASK_BACKEND=postgres_rq`、`TOOL_RUNTIME=docker` 等）。**必须**为 `ASSEMBLY_DATA_ROOT` 设置宿主机绝对路径（工具容器同路径 bind）。

账号文件：

```bash
sudo cp deploy/users.json.example /etc/bacterial-assembly/users.json
sudo chmod 600 /etc/bacterial-assembly/users.json
/opt/bacterial-assembly/.venv/bin/python web/password_hash.py   # 填入哈希
```

## 6. 数据目录

默认示例：`/srv/bacterial-assembly/data`（与 env 一致）。布局见 ADR 0009。属主应为服务用户 `bacterial`。

## 7. 数据库迁移

```bash
export $(sudo grep -v '^#' /etc/bacterial-assembly/assembly.env | xargs)
export PYTHONPATH=/opt/bacterial-assembly
/opt/bacterial-assembly/.venv/bin/python -m web.migrate_pg
```

## 8. Systemd

单元模板在 `deploy/systemd/`。安装脚本会按实际路径写入 `/etc/systemd/system/`：

- `bacterial-web.service` — Gunicorn `127.0.0.1:8000` / `web.app:app`
- `bacterial-worker-qc.service` — `python -m web.rq_worker qc`
- `bacterial-worker-assembly.service` — `python -m web.rq_worker assembly`

```bash
sudo ./deploy/install_host.sh --repo /opt/bacterial-assembly --data-root /srv/bacterial-assembly/data
# 填好 assembly.env 中的密钥与 DATABASE_URL 后可：
sudo ./deploy/install_host.sh --start
```

管理：

```bash
sudo ./deploy/start.sh
sudo ./deploy/status.sh
sudo ./deploy/logs.sh
sudo ./deploy/stop.sh
```

上述脚本**不会**停止 PostgreSQL、Redis、Docker 或 Nginx。

### Docker 组安全

服务用户加入 `docker` 组后方能调用 Engine（Worker 跑工具；Web 取消任务时也可能停容器）。docker 组成员实质上可取得宿主机高权限；仅授可信运维账户，并限制 socket 暴露。

## 9. Nginx

```bash
sudo cp deploy/nginx/bacterial-assembly.conf /etc/nginx/conf.d/
# 修改 server_name 与证书路径
sudo nginx -t && sudo systemctl reload nginx
```

反代至 `127.0.0.1:8000`；勿对公网开放 Gunicorn、PostgreSQL、Redis 或 Docker socket。正式环境使用 HTTPS，并保持 `ASSEMBLY_SESSION_COOKIE_SECURE=true`。

## 10. 访问与检查

- Web：`https://<你的域名>/`（经 Nginx）
- Worker：`systemctl status bacterial-worker-qc bacterial-worker-assembly`
- 工具是否走容器：任务日志 / `docker ps` 应出现 `bacterial-assembly-*` 容器；宿主机 PATH 上不应依赖同名生信二进制

## 11. 升级

1. `git pull`（或同步代码）
2. `pip install -r requirements-host.txt`
3. 如有 SQL：`python -m web.migrate_pg`
4. 工具版本变更：`./docker/tools/build.sh` 后重启 Worker
5. `./deploy/restart.sh`

## 12. 备份与恢复

备份：`ASSEMBLY_DATA_ROOT` 全树、PostgreSQL dump、`/etc/bacterial-assembly/`（env + users）。恢复后先起 PG/Redis，再 `migrate_pg`（若需要），再 start 服务。

## 13. 常见问题

| 现象 | 排查 |
| --- | --- |
| 上传记录校验失败 | `ValidateFastq` / Java 是否在服务 `PATH` |
| 工具容器起不来 | `docker info`；用户是否在 `docker` 组；镜像是否已 build |
| 容器写权限错误 | 数据目录属主与容器内 `uid:gid`（与 Worker 进程一致） |
| 取消不停容器 | Web 与 Worker 均需 `TOOL_RUNTIME=docker` 且能访问 Docker API |
| 会话 cookie 异常 | HTTPS 反代头；`ASSEMBLY_SESSION_COOKIE_SECURE` |

工作台账号与任务状态说明见 [web_deployment.md](web_deployment.md)。安全见 [security.md](security.md)。
