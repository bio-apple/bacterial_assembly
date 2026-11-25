---
status: accepted
---

# 宿主机运行 Web/Worker；仅生信工具使用 Docker

推荐部署将 Flask、Gunicorn、RQ Worker、PostgreSQL 与 Redis 客户端跑在 Linux 宿主机（systemd + 系统或外部 PG/Redis）；仅 fastp / 组装器 / QUAST 使用白名单工具镜像（ADR 0010）。不再构建或依赖应用容器镜像，并移除 Docker Compose 全栈部署。服务账户 `bacterial` 加入 `docker` 组以调用 Engine（含 Web 取消路径停容器）；docker 组成员权限接近 root，须在部署文档中明示。ValidateFastq / OpenJDK 由宿主机 micromamba 环境提供，与 pip venv 分离。
