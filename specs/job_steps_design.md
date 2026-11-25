## Feature: 组装步骤的独立执行记录

`jobs.status` 只表示整次任务。一次任务会按选择顺序跑 fastp、每个组装器、该组装器的 QUAST，以及比较汇总。某个组装器失败时，其余步骤继续；任务终态可以是已完成、部分完成或失败。

### [Frontend]

- 任务页有「分析步骤」表，一行一个实际步骤，右侧是状态标签。
- 失败原因和退出码只在数据库里有记录时出现。没有步骤记录时说明还没有，不补成功或失败。
- 任务轮询时用同一份数据重画这张表。标签来自服务器，页面不解释原始状态词。

### [Backend]

- 表 `job_steps`：`job_id`、`step_name`、`status`、`started_at`、`completed_at`、`exit_code`、`error`，`(job_id, step_name)` 唯一。
- `step_name` 只接受这次任务计划内的步骤编号，例如 `fastp`、`spades`、`spades:quast:original`、`comparison`。
- 步骤状态只用 `queued`、`running`、`succeeded`、`failed`、`cancelled`、`skipped`。任务终态另有 `partial_success`，不是步骤状态。
- `postgres_rq` 下步骤真源在 PostgreSQL；Web 经 `load_recorded_steps` 读取，并可投影到 SQLite。`stages.json` 可写入供排障。已结束的步骤不会被改回排队。
- 任务已经结束而某步仍是排队，记为 `skipped`。任务为部分完成而某步仍是运行中，记为 `failed`。用户取消时，还在排队或进行中的步骤记为 `cancelled`。
- 不提供单步重跑。

### [Security]

- 步骤只跟所属任务一起返回。读任务的接口仍要求登录，并且只返回当前用户的任务。
- 没有让浏览器写入步骤的接口。
- 查询使用参数绑定。失败原因按文本显示，并截断。
- 不记录密码或会话内容。同步失败只进服务器日志。
