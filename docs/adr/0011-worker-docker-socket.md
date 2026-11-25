---
status: accepted
---

# Worker（与 Web 取消路径）经 Docker socket 启动/停止工具容器

宿主机上的 Worker（及由其拉起的流水线）通过 Docker socket 使用 Docker Engine；客户端采用 Docker SDK（docker-py）以参数列表启动容器，不引入独立代理或编排系统，不用 shell 拼接。生信工具容器不得挂载该 socket，不得 privileged。工具容器运行用户与数据卷 UID/GID 对齐。服务账户加入 `docker` 组以访问 socket；接受同机部署下的 socket 权限风险，以换取最小基础设施改动。取消任务时 Web 也可能调用 `stop_job_containers`，故 Web 进程同样需要 Docker API 访问（ADR 0020）。若日后合规禁止 socket，再改为受限代理。
