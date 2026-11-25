---
status: accepted
---

# 运行数据按生命周期分目录

Web 与 worker 的运行数据留在 `ASSEMBLY_DATA_ROOT`（Compose 里是 `/data`，对应 `deploy/data/`），不放进 Web 代码目录。未完成的上传在 `uploads/incoming`，分片在 `uploads/chunks`，校验通过但尚未绑定任务的文件在 `uploads/validated`。分析只读 `jobs/<id>/input`。worker 先把 `--outdir` 指到 `jobs/<id>/work`，结束再发布到 `jobs/<id>/output`。日志在 `jobs/<id>/logs`，下载包在 `archive/<id>.tar.gz`，队列库是 `database/jobs.sqlite3`，临时目录是 `temp/`。

接收中的字节和已经提交的 reads 分开存放，清理临时文件或迁到独立磁盘时不易误删结果。若数据根上还有 `jobs.sqlite3`、而 `database/jobs.sqlite3` 不存在，启动时只移动这个库文件，不删表。队列锁放在数据根的 `worker.lock`，避免两个 worker 同时执行任务。
