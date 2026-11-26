---
status: accepted
---

# 架构深化：Web 分层与目录蓝图

本波次把 `job_support` 拆成序列化、磁盘产物与展示三层；把创建/重新分析与入队之间的共享流程收成**分析任务创建**；路由按 uploads、jobs、catalog 三个 Blueprint 注册，端点名保持不变。

## 序列化与产物

- `web.job_serialize`：任务行序列化、状态标签、历史筛选与时长展示。
- `web.job_artifacts`：输出目录、比较表、fastp、输入可复制性；长度口径缺评估仅在磁盘上存在过滤后 contig 且无 `report.tsv` 时记阈值缺席，不读工作项状态。
- `web.job_presentation`：`job_detail` / `job_report_model` 与样本–批次关联；`detail` 不再携带 `steps`，页面与轮询统一用 `work_items`。

## 任务创建与路由

- `web.analysis_task_intake`：新建与 rerun 共用的目录准备、样本/批次与 jobs 插入，再调用 `submit_analysis_task`（ADR 0005 rerun 为新任务，ADR 0021 入队与补偿）。
- `web.routes.catalog`：总览、模块、历史、比较与 `GET /api/jobs`。
- 上传 helpers 自 `web.upload_io` / `web.report_io` 直接引用；删除 `job_support` 与 `recorded_steps` 等薄包装。

## 计划骨架

- `scripts.pipeline_plan` 承载 `planned_stages`；`stage_record` 仅写盘并 re-export，供 Worker 兼容旧 import。

## 契约

- 任务级 `JOB_STATUS_LABELS` 不含 `skipped`（步骤态仍可 skipped）。
- 样式变量统一 `--brand` / `--brand-soft`；成功表单提示用 `--success`。
- 比较口径缺席只看磁盘产物，不读工作项状态。