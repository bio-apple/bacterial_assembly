---
status: superseded by ADR-0010
---

# 应用镜像含运行依赖

应用镜像基于 `anaconda/miniconda`，安装运行依赖（含可选本地生信二进制以支持 `TOOL_RUNTIME=local`），不把仓库源码打进镜像。运行时只读挂载仓库到 `/opt/bacterial-assembly`。生信正式执行使用独立工具镜像（ADR 0010）。构建与运行按 `linux/amd64`。
