---
status: superseded by ADR 0020
---

# 应用镜像仅含运行依赖（已废止）

曾规定应用镜像基于 conda/miniconda，仅含 Web/Worker 与 ValidateFastq。ADR 0020 起推荐部署改为宿主机运行时，不再构建应用镜像；生信步骤仍在工具侧车中（ADR 0010）。
