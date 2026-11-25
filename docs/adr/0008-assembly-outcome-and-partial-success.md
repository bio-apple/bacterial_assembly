---
status: accepted
---

# 用组装结果声明判定任务终态

流程在正常结束、部分组装方法失败，或前置步骤失败时写出 `assembly_outcome.json`。内容至少包含协议版本、状态、所选/成功/失败的组装方法和退出码；先写临时文件再原子替换。

全部方法成功为 `succeeded`（退出码 0）；部分成功为 `partial_success`（退出码 3）；全部失败或 fastp 等前置失败为 `failed`（退出码 1）。Worker 同时核对声明文件、进程退出码和对应 contig 文件；不能只凭 `comparison.tsv` 是否存在判定成功。Web 任务终态与声明一致，部分完成时可下载已有结果。
