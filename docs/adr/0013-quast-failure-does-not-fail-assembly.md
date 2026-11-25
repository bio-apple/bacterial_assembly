---
status: accepted
---

# QUAST 失败不判定组装方法失败

组装方法在写出约定 contig（并完成长度过滤）后即视为该方法成功，写入组装结果声明的成功列表。随后各长度口径的 QUAST 记为评估步骤；评估失败只反映在阶段记录中，不得把该方法改回失败，不得删除已有 FASTA，不得仅因评估失败把任务终态打成 `failed`。这改变当前 `run_assembler()` 在 QUAST 异常时 `return False` 的行为，以区分组装成败与评估成败。
