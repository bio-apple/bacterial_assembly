---
status: accepted
---

# 步骤读模型接管进度与报告失败阶段

Web 读路径以步骤读模型投影工作项（`work_items`）、流水线时间线（`progress.groups`）与失败阶段；状态词一律为步骤状态。`stages.json` 仅供 Worker 写盘排障，Web 不再 `load_stage_record` / `build_progress`。比较口径缺席由作业目录产物判断。上传字节进 `web.upload_io`，报告落盘进 `web.report_io`。本波 HTTP 抽出 `uploads` 与 `jobs` blueprint；其余页面仍在 `create_app`。工作台 CSS 真源为 `web/static/style.css`，经 `scripts/sync_pages_style.sh` 同步到 `assets/style.css`。
