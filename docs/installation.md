# 安装与环境

## 应用镜像

```bash
./docker/build-app.sh
# 可选覆盖：./docker/build-app.sh --platform linux/amd64
# 或：PLATFORM=linux/arm64 ./docker/build-app.sh
```

构建平台解析（ADR 0019）：`--platform` > 环境变量 `PLATFORM` > 宿主机 `uname -m`。支持：`linux/amd64`、`linux/arm64`。

基础镜像是 `anaconda/miniconda:26.9.4`。镜像安装 Web/Worker 运行依赖与 ValidateFastq，**不含** fastp / 组装软件 / QUAST。生信步骤使用独立工具镜像（见 [docker_tools.md](docker_tools.md)）。不把仓库源码打进镜像。

应用镜像内固定安装：

| 软件 | 版本 / 说明 |
| --- | --- |
| Python | 3.10 |
| Flask / Gunicorn | 3.1.1 / 23.0.0 |
| ValidateFastq | 0.1.1 |
| OpenJDK | 8 |
| docker / psycopg / redis / rq | 编排与队列客户端 |

Compose 用 `micromamba run -n base` 启动 Web 与 Worker。仓库只读挂载到 `/opt/bacterial-assembly`；修改流程或 Web 代码后重启服务即可。工具镜像变更时重新执行 `./docker/tools/build.sh`（与应用镜像同一构建平台）。


`TOOL_RUNTIME=local` 时须在宿主机（或当前 PATH）自行提供生信二进制；应用镜像不提供这些程序。

## 本地测试依赖

```bash
python3 -m pip install -r requirements-web.txt
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh
```

Web 部署步骤见 [web_deployment.md](web_deployment.md)。Compose 见 [docker_deployment.md](docker_deployment.md)。
