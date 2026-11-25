---
status: accepted
---

# 应用镜像仅含运行依赖

应用镜像基于 `anaconda/miniconda`，安装 Web/Worker 运行依赖（Python、Flask、Gunicorn、编排库）与上传校验用的 ValidateFastq / OpenJDK。不内嵌 fastp、组装软件或 QUAST；生信步骤在工具侧车镜像中执行（ADR 0010）。不把仓库源码打进镜像；运行时只读挂载仓库到 `/opt/bacterial-assembly`。构建平台见 ADR 0019（宿主机判定，可覆盖；`linux/amd64` 或 `linux/arm64`）。
