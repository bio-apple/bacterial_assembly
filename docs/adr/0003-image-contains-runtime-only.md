---
status: accepted
---

# 镜像只包含软件环境

Docker 镜像基于 `anaconda/miniconda:26.9.4`，只安装 conda/micromamba 软件环境，不把流程和 Web 代码复制进镜像。运行时把仓库只读挂载到 `/opt/bacterial-assembly`。命令行显式调用其中的 `run_pipeline.py`；Compose 的 Web 和 worker 以 `micromamba run -n base` 启动，工作目录仍是该挂载路径。

环境安装慢，代码用挂载即可在重启后生效。Flask 和 Gunicorn 留在镜像里，因为它们是运行依赖，不是项目源码。Bioconda 中 SPAdes 3.15.5、QUAST 5.2.0 和 Shovill 依赖的 FLASH 只有 linux-64 构建，因此构建与运行按 `linux/amd64`。
