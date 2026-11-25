---
status: accepted
---

# 组装方法串行并按九成资源尝试

同一分析任务内的组装方法按选择顺序逐个执行。逻辑 CPU 与物理内存先取容器 cgroup（v1 或 v2）的有效上限，读不到时回退到主机。fastp 与每种组装方法共用同一配额：CPU 的 90%（向下取整，至少 1）和内存的 90%（按 GiB 向下取整，至少 1 GiB）。分配写入 `run.log`。样本任务仍按 FIFO 串行。一个方法失败不取消后续方法。

资源通过工具参数传递：SPAdes 的 `-m`，Shovill 的 `--ram`，MEGAHIT 的 `--memory`，Unicycler 的 `--spades_options -m`。不另起 cgroup 去限制子进程。Unicycler 内部仍把 SPAdes 线程限制为最多 32；Shovill 超过 16 线程时仍可能不稳定，调用仍按 90% 线程传递。独立运行 QUAST 且未由总控分配线程时，使用全部逻辑 CPU。
