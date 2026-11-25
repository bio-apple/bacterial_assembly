# Web 部署

工作台定位为「微生物 NGS 数据分析系统」。用户登录后进入 `/overview`，再从 `/modules` 进入细菌基因组组装模块。

完整宿主机安装见 [host_deployment.md](host_deployment.md)。

## 账号与密钥

```bash
sudo cp deploy/assembly.env.example /etc/bacterial-assembly/assembly.env
sudo cp deploy/users.json.example /etc/bacterial-assembly/users.json
sudo chmod 600 /etc/bacterial-assembly/assembly.env /etc/bacterial-assembly/users.json
```

设置 `ASSEMBLY_WEB_SECRET`、`ASSEMBLY_SESSION_COOKIE_SECURE`、`ASSEMBLY_USERS_FILE`、`ASSEMBLY_DATA_ROOT`、`DATABASE_URL`、`REDIS_URL` 等。经本机 Nginx HTTPS 反代访问时保持 `ASSEMBLY_SESSION_COOKIE_SECURE=true`；若浏览器直接访问 `http://127.0.0.1:8000`，改为 `false`。生成密码哈希：

```bash
/opt/bacterial-assembly/.venv/bin/python /opt/bacterial-assembly/web/password_hash.py
```

把账号与 scrypt 哈希填入 `users.json`。账号由管理员预配，不提供公开注册。

## 进程与数据根

- Gunicorn：`127.0.0.1:8000`，`web.app:app`
- Worker：`python -m web.rq_worker qc` / `assembly`
- 数据根：`ASSEMBLY_DATA_ROOT`（绝对路径），布局见 ADR 0009：

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

全部组装方法成功为已完成；至少一种成功且至少一种失败为部分完成（可下载已有结果）。可取消 `queued` 或 `running`。任务状态描述整次分析；单方法成败见 `assembly_outcome.json` 与步骤记录（任务页步骤读模型见 [architecture.md](architecture.md) / CONTEXT）。

`/history` 按账号隔离；可下载结果或日志。重新分析新建任务；`TASK_BACKEND=postgres_rq` 时创建与重新分析均经 `enqueue_rq_pipeline` 入队。

## 模块扩展

`/modules` 由 `web/modules.py` 中的 `ANALYSIS_MODULES` 驱动。安全与数据清理见 [security.md](security.md)。术语见 [CONTEXT.md](../CONTEXT.md)。
