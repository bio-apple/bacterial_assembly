---
status: superseded by ADR-0016
---

# 组装调度与资源配额

见 ADR 0016（组装工作项默认串行）与 ADR 0012（工具参数与容器双限制）。fastp 与各组装方法使用约 90% 的有效 CPU/内存配额，经工具参数传递（如 SPAdes `-m`、Shovill `--ram`、MEGAHIT `--memory`、Unicycler `--spades_options -m`）。
