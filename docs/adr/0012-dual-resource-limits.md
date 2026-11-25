---
status: accepted
---

# 工具参数与容器 cgroup 双限制

`resource_budget` / `resource_limits` 的约 90% 配额写入各工具自身的线程与内存参数。`TOOL_RUNTIME=docker` 时，DockerExecutor 再为容器设置 CPU 上限，内存上限为 `method_memory_gib + 1` GiB（相对工具已获配额再留至少 1 GiB）。工具参数限制与容器 cgroup 限制同时生效。
