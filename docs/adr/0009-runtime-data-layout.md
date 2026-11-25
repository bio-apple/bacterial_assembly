---
status: accepted
---

# 运行数据按生命周期分目录

Web 与 Worker 的运行数据在 `ASSEMBLY_DATA_ROOT`（宿主机绝对路径，由 EnvironmentFile 配置；示例 `/srv/bacterial-assembly/data`），不放进 Web 代码目录。
分片上传在 `uploads/chunks`，直传暂存在 `uploads/incoming`，校验通过待绑定在 `uploads/validated`。分析只读 `jobs/<id>/input`；运行写入 `jobs/<id>/work`，结束后发布到 `jobs/<id>/output`。日志在 `jobs/<id>/logs`，下载包在 `archive/<id>.tar.gz`，临时目录 `temp/`。Web 读模型库为 `database/jobs.sqlite3`；步骤调度状态在 PostgreSQL。组装 Worker 与 QC Worker 挂载同一数据根。
