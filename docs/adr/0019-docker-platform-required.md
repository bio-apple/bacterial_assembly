---
status: accepted
---

# Docker 构建平台：宿主机判定，可显式覆盖

自建应用镜像与全部生信工具侧车镜像必须按明确的 **构建平台** 构建；Compose 与 Dockerfile 不写死 `platform`。支持 `linux/amd64` 与 `linux/arm64`。解析优先级：CLI `--platform` > 环境变量 `PLATFORM` > 宿主机 `uname -m`（`x86_64`/`amd64`→`linux/amd64`，`arm64`/`aarch64`→`linux/arm64`）；无法映射或取值非法时失败。共享逻辑在 `docker/lib/resolve-platform.sh`；入口为 `./docker/build-app.sh`、`./docker/tools/build.sh`、`./deploy/build.sh`（后者仅 Compose 应用镜像）。应用与工具镜像须同一构建平台；镜像 tag 不带平台后缀。Rosetta 等导致 `uname -m` 与真实硬件不一致时，应显式传 `--platform`。
