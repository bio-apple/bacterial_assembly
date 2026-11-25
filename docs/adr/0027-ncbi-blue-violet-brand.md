---
status: accepted
supersedes: "0026"
---

# 主体色改为 NCBI 蓝紫系

工作台与 Pages 演示的唯一品牌主色由青绿改为 NCBI 语境的蓝紫：`#3C4F9C`（brand）/ `#2A3A7A`（deep）/ `#1A2248`（sidebar）。保留左侧 Sidebar 壳层与现有布局节奏；语义色（成功绿、失败红、警告琥珀）不变。`web/static/style.css` 经同步脚本投影到 `assets/style.css`。相对 ADR 0023/0026 的青绿品牌决策，本 ADR 仅重开主色，不重开导航壳。
