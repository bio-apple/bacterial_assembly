# 安全与数据管理

## HTTPS 反向代理

将 `deploy/nginx/bacterial-assembly.conf` 安装到 Nginx `conf.d`，替换证书路径与服务器名后重载。防火墙只开放 HTTPS；不要把应用的 8000 端口、PostgreSQL、Redis 或 Docker socket 暴露到公网。

登录使用 HttpOnly、SameSite=Lax 的会话 cookie，并验证 CSRF。`ASSEMBLY_SESSION_COOKIE_SECURE` 默认为 `true`（适合 HTTPS 反代）；浏览器若直连 `http://127.0.0.1:8000`，须在 `assembly.env` 设为 `false`。应用信任一跳 `X-Forwarded-*` 头；生产环境只应通过可信的本机反向代理访问 Gunicorn。

## 账号

账号与密码哈希保存在 `ASSEMBLY_USERS_FILE`（示例 `/etc/bacterial-assembly/users.json`，scrypt）。密钥在 `assembly.env` 的 `ASSEMBLY_WEB_SECRET`。不提供注册或自助删号。任务、日志和结果只对提交该任务的账号开放。

## Docker 组

服务用户加入 `docker` 组后方能启动/停止工具容器。docker 组成员可取得宿主机高权限；仅授可信账户，并监控 socket 访问。

## 上传路径

会话编号和暂存路径由服务器生成。请求若携带路径字段或试图穿越目录，会被拒绝。未完成或校验失败的文件不会进入 `validated` 或任务 `input`。

## 结果保留与清理

上传 FASTQ、作业数据库、日志、分析结果和下载压缩包一直保留到管理员清理；Web 界面不提供删除。完全清理前先停止服务，再手动归档或删除 `ASSEMBLY_DATA_ROOT` 中的数据。删除其中的 `database/jobs.sqlite3` 会一并移除作业历史。
