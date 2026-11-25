# 安装与环境

推荐部署见 [host_deployment.md](host_deployment.md)（宿主机 Web/Worker + 工具 Docker）。

## 宿主机 Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-host.txt
```

`requirements-host.txt` 引用与 `requirements-web.txt` 相同的 Web/Worker 钉版本。ValidateFastq / OpenJDK 另用 micromamba 安装（见宿主机部署文档），**不要**在宿主机安装 SPAdes、fastp、QUAST 等组装工具。

## 生信工具镜像

```bash
./docker/tools/build.sh
```

构建平台：`--platform` > `PLATFORM` > 宿主机判定（ADR 0019）。支持 `linux/amd64`、`linux/arm64`。

## 本地测试依赖

```bash
python3 -m pip install -r requirements-web.txt
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh
```

Web 工作台说明见 [web_deployment.md](web_deployment.md)。
