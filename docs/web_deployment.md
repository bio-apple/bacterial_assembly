# Web 部署

工作台定位为「微生物 NGS 数据分析系统」。用户登录后进入 `/overview`，再从 `/modules` 进入细菌基因组组装模块。

完整 Compose（Postgres、Redis、双 Worker、工具侧车）见 [docker_deployment.md](docker_deployment.md)。

## 前置条件

Linux 服务器安装 Docker Engine、Docker Compose plugin、Nginx，并将本仓库放到服务器。

## 账号与密钥

```bash
cd deploy
cp .env.example .env
cp users.json.example users.json
chmod 600 .env users.json
```

在 `.env` 中设置 `ASSEMBLY_WEB_SECRET`、`ASSEMBLY_SESSION_COOKIE_SECURE`、`APP_UID`、`APP_GID`、`POSTGRES_PASSWORD`、`DOCKER_GID` 等。经本机 Nginx HTTPS 反代访问时保持 `ASSEMBLY_SESSION_COOKIE_SECURE=true`；若浏览器直接访问 `http://127.0.0.1:8000`，改为 `false`。生成密码哈希：

```bash
docker compose build
docker compose run --rm --entrypoint micromamba web \
  run -n base python /opt/bacterial-assembly/web/password_hash.py
```

把账号与 scrypt 哈希填入 `users.json`。账号由管理员预配，不提供公开注册。

## 启动

见 [docker_deployment.md](docker_deployment.md)。仓库只读挂载到 `/opt/bacterial-assembly`。Web 绑定 `127.0.0.1:8000`。数据根 `deploy/data/`（容器内 `/data`）：

```text
data/
  uploads/chunks/
  uploads/incoming/
  uploads/validated/
  jobs/<任务编号>/input/
  jobs/<任务编号>/work/
  jobs/<任务编号>/output/
  jobs/<任务编号>/logs/
  reports/
  database/jobs.sqlite3
  temp/
  archive/
```

分片上传在 `uploads/chunks/`；直传暂存在 `uploads/incoming/`；校验通过待绑定在 `uploads/validated/`。分析只读 `jobs/<id>/input`。

## 上传与校验

浏览器按 8 MiB 分片上传 R1/R2。上传库表状态为 `pending` / `uploading` / `completed` / `failed` / `consumed`：

1. **字节校验**：SHA-256 与浏览器提交值一致  
2. **记录校验**：`.gz` 完整 gzip；ValidateFastq（退出码 0，且输出含 `no errors found`）→ `completed`  
3. **配对检查**：创建或重新分析前核对 R1/R2  

任一步失败都不能启动组装。

## 任务状态

| 英文 | 中文 |
| --- | --- |
| `queued` | 排队中 |
| `running` | 分析中 |
| `succeeded` | 已完成 |
| `partial_success` | 部分完成 |
| `failed` | 失败 |
| `cancelling` | 取消中 |
| `cancelled` | 已取消 |

全部组装方法成功为已完成；至少一种成功且至少一种失败为部分完成（可下载已有结果）。可取消 `queued` 或 `running`。任务状态描述整次分析；单方法成败见 `assembly_outcome.json` 与步骤记录。

`/history` 按账号隔离；可下载结果或日志；重新分析会新建任务。

## 模块扩展

`/modules` 由 `web/modules.py` 中的 `ANALYSIS_MODULES` 驱动。安全与数据清理见 [security.md](security.md)。术语见 [CONTEXT.md](../CONTEXT.md)。
