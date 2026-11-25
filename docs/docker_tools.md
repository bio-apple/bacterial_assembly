# 生信工具镜像

fastp、四种组装软件和 QUAST 在独立工具容器中运行。应用进程（Web / Worker）通过 Docker Engine 启动容器；账号与挂载由注册表白名单约束。

## 镜像

| 工具 | 镜像 |
| --- | --- |
| fastp | `bacterial-assembly-fastp:latest` |
| SPAdes | `bacterial-assembly-spades:latest` |
| Unicycler（含 SPAdes） | `bacterial-assembly-unicycler:latest` |
| Shovill | `bacterial-assembly-shovill:latest` |
| MEGAHIT | `bacterial-assembly-megahit:latest` |
| QUAST | `bacterial-assembly-quast:latest` |

配置：`config/software_registry.yaml`。基础镜像为 `anaconda/miniconda`（不钉 tag）；conda 包在 Dockerfile 中不钉版本；构建时取频道当前解。运行后的具体版本见任务 `versions.txt`。
## 构建

平台由 `docker/lib/resolve-platform.sh` 解析（ADR 0019）：`--platform` > `PLATFORM` > 宿主机判定。支持：`linux/amd64`、`linux/arm64`。Dockerfile 不写死平台。

```bash
./docker/tools/build.sh
# 覆盖示例：./docker/tools/build.sh --platform linux/amd64
```

## 环境变量

| 变量 | 含义 |
| --- | --- |
| `TOOL_RUNTIME` | `docker` 或 `local`（未设置时为 `local`；生产 EnvironmentFile 为 `docker`） |
| `_ASSEMBLY_JOB_ID` | 容器标签 `bacterial.assembly.job_id` |
| `_ASSEMBLY_STEP_NAME` | 容器标签 `bacterial.assembly.step` |
| `_ASSEMBLY_STEP_ID` | 写入 Postgres `job_steps.container_id` 的步骤主键 |
| `_ASSEMBLY_ASSIGNED_THREADS` | 工具线程参数 |
| `_ASSEMBLY_METHOD_MEMORY_GIB` | 工具内存参数；容器 limit = 该值 + 1 GiB |

工具容器：`network_mode=none`、非 privileged、不挂 Docker socket；按任务目录同路径 bind；UID/GID 与数据卷对齐。

## 调用

`scripts/common.run_command` / `record_version`：注册表内命令且 `TOOL_RUNTIME=docker` 时走 `scripts/docker_executor.py`。contig 文件名与 QUAST 目录约定见 `scripts/pipeline_core.py`。contig 过滤与 `summarize_quast.py` 在应用进程内执行。

## 取消

停止相关工具容器（按 job 标签），并更新任务 / 步骤状态。组装已成功时，QUAST 失败只记评估失败，不改组装成败。
