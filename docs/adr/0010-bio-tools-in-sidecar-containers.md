---
status: accepted
---

# 生信工具在侧车容器中运行

fastp、四种组装软件和 QUAST 各自在独立工具镜像的容器中执行；Flask 与 Worker 在宿主机应用运行时中执行（ADR 0020），不含这些生信二进制。`TOOL_RUNTIME=docker|local`：推荐部署使用 `docker`；`local` 表示在当前进程树内调用宿主机已安装的二进制。一工具一镜像，命名 `bacterial-assembly-<tool>:<上游版本>`；Unicycler 镜像含其所需的 SPAdes 4.0.0。调用切面在 `scripts/common.run_command()`，由注册表选择 docker/local，禁止用户指定镜像或挂载。工具容器按宿主机同路径 bind 任务目录；contig 过滤与 `summarize_quast.py` 在应用进程内执行。镜像构建平台见 ADR 0019（宿主机判定，可覆盖；`linux/amd64` 或 `linux/arm64`）。
