---
status: accepted
---

# Worker 经 Docker socket 启动工具容器

Worker（及由其拉起的 Pipeline）通过挂载的 Docker socket 使用 Docker Engine，客户端采用 Docker SDK（docker-py）以参数列表启动容器，不引入独立代理或编排系统，不用 shell 拼接。生信工具容器不得挂载该 socket，不得 privileged。工具容器运行用户与数据卷 UID/GID 对齐。接受同机 Compose 部署下的 socket 权限风险，以换取最小基础设施改动；若日后合规禁止 socket，再改为受限代理。
