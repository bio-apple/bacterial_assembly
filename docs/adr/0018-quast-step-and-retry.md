---
status: accepted
---

# QUAST 工作项与重试边界

每种成功组装对应 **一个** QUAST 工作项，工作项内按 original / gt500 / gt1000 串行评估。评估失败只更新步骤状态，不把对应组装方法改回失败，也不单独把 Job 打成 `failed`（与 ADR 0013 及 `partial_success` 规则一致）。自动重试仅针对基础设施类失败（如 Docker Engine 暂不可用、瞬时 IO），默认最多 3 次；工具非零退出、输入损坏、参数错误与用户取消不自动重试。
