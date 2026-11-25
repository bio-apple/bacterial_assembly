---
status: accepted
---

# 宿主机运行 Web/Worker；仅生信工具使用 Docker

Flask、Gunicorn、RQ Worker 以及 PostgreSQL / Redis 客户端跑在 Linux 宿主机（systemd；PG/Redis 为系统实例或外部 URL）。仅 fastp、组装器与 QUAST 使用白名单工具镜像（ADR 0010）。服务账户 `bacterial` 加入 `docker` 组以调用 Engine（含 Web 取消路径停容器）；docker 组成员权限接近 root，部署文档须写明。ValidateFastq 与 OpenJDK 由宿主机 micromamba 环境提供，与 pip venv 分离。
