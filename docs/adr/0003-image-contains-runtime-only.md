---
status: accepted
---

# 镜像只包含软件环境

Docker 镜像只安装两套 micromamba 环境，不把流程和 Web 代码复制进镜像。运行时把仓库只读挂载到 `/opt/bacterial-assembly`。命令行显式调用其中的 `run_pipeline.py`；Compose 的 Web 和 worker 仍以该目录为工作目录。

把代码打进镜像可以让一条 `docker run` 自包含，但每次改流程都要重建镜像。环境安装才是慢的部分，代码用挂载即可在重启后生效。Flask 和 Gunicorn 留在镜像里，因为它们是运行依赖，不是项目源码。
