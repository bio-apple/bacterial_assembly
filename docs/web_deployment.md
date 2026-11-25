# Web 部署

工作台定位为「微生物 NGS 数据分析系统」。用户登录后进入 `/overview`，再从 `/modules` 进入细菌基因组组装模块。

## 前置条件

Linux 服务器安装 Docker Engine、Docker Compose plugin、Nginx，并将本仓库放到服务器。

## 账号与密钥

```bash
cd deploy
cp .env.example .env
cp users.json.example users.json
chmod 600 .env users.json
```

在 `.env` 中设置 `ASSEMBLY_WEB_SECRET`、`APP_UID`、`APP_GID`（密钥无默认值）。生成密码哈希：

```bash
docker compose build
docker compose run --rm --entrypoint micromamba web \
  run -n base python /opt/bacterial-assembly/web/password_hash.py
```

把账号与 scrypt 哈希填入 `users.json`。账号由管理员预配，不提供公开注册。

## 启动

```bash
mkdir -p data
sudo chown "$(id -u):$(id -g)" data
docker compose up -d --build
docker compose logs -f web worker
```

Compose 将仓库只读挂载到 `/opt/bacterial-assembly`。Web 端口绑定 `127.0.0.1:8000`。运行数据根目录是 `deploy/data/`（容器内 `/data`，`ASSEMBLY_DATA_ROOT`）：

```text
data/
  uploads/incoming/
  uploads/chunks/
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

分析只读 `jobs/<任务编号>/input`。若数据根下仍有旧的 `jobs.sqlite3` 而 `database/jobs.sqlite3` 不存在，启动时会把旧库移过去，不删表。worker 使用数据根上的 `worker.lock`；不要横向扩容 worker。

## 上传与校验

浏览器按 8 MiB 分片上传 R1/R2：

1. **字节校验**：SHA-256 与浏览器提交值一致  
2. **记录校验**：`.gz` 完整 gzip 读取；ValidateFastq 0.1.1（退出码 0，且输出含 `no errors found`，不含 `Error found at readnumber`）→ 单文件可分析  
3. **配对检查**：创建或重新分析前核对 R1/R2 数量与 read 编号  

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

全部组装方法成功为已完成；至少一种成功且至少一种失败为部分完成（可下载已有结果）；全部失败或前置失败为失败。用户请求取消后、进程未停妥为取消中；取消完成为已取消。可取消 `queued` 或 `running`；终态任务不可再取消。不为取消临时打包。

任务状态描述整次分析；单个组装方法成败写在 `assembly_outcome.json` 与步骤记录中。`job_steps`：终态任务上仍排队的步骤为 `skipped`；部分完成时仍运行的步骤为 `failed`。不提供单步重跑。

`/history` 按账号隔离；可下载结果或日志，重新分析会新建任务。

## 模块扩展

`/modules` 由 `web/modules.py` 中的 `ANALYSIS_MODULES` 驱动。新增模块需先实现登录保护路由和页面，再登记模块 ID。页面沿用 `web/templates/components/` 中的组件。

安全与数据清理见 [security.md](security.md)。术语见仓库根目录 [CONTEXT.md](../CONTEXT.md)。
