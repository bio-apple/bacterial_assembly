# 安装与环境

部署步骤见 [host_deployment.md](host_deployment.md)。

## Python 依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-host.txt
```

`requirements-host.txt` 与 `requirements-web.txt` 钉同一组 Web/Worker 依赖。ValidateFastq / OpenJDK 用 micromamba 安装（见宿主机部署文档）。宿主机不安装 SPAdes、fastp、QUAST 等组装工具。

## 生信工具镜像

```bash
./docker/tools/build.sh
```

构建平台：`--platform` > `PLATFORM` > 宿主机判定（ADR 0019）。支持 `linux/amd64`、`linux/arm64`。

## 测试

```bash
python3 -m pip install -r requirements-web.txt
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh
```

工作台账号与任务状态见 [web_deployment.md](web_deployment.md)。
