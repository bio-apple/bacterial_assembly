---
status: accepted
---

# 工具参数与容器 cgroup 双限制

继续用现有 `resource_budget` / `resource_limits` 的 90% 配额写入各工具自身的线程与内存参数（ADR 0002）。在 `TOOL_RUNTIME=docker` 时，DockerExecutor 再为容器设置 CPU 上限，内存上限为 `method_memory_gib + 1` GiB（相对工具已获配额再留至少 1 GiB），避免内核直接杀容器却无工具侧诊断。不取消工具参数限制，也不改为「只靠 cgroup、不再传 `-m`/`--ram` 等」。
