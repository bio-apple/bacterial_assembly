# 安装与环境

## Docker 镜像

```bash
docker build --platform linux/amd64 -t bacterial-assembly:latest .
```

基础镜像是 `anaconda/miniconda:26.9.4`。镜像只安装软件环境，不包含流程和 Web 代码。arm64 宿主机也请按 `linux/amd64` 构建：SPAdes 3.15.5、QUAST 5.2.0 和 Shovill 依赖的 FLASH 只有 linux-64 包。

镜像内固定安装：

| 软件 | 版本 / 说明 |
| --- | --- |
| fastp | 0.23.4 |
| SPAdes | 3.15.5（base） |
| Unicycler | 0.5.1（独立 `unicycler` 环境，含 SPAdes 4.0.0） |
| Shovill | 1.4.2 |
| SKESA | 2.5.1 |
| MEGAHIT | 1.2.9 |
| QUAST | 5.2.0 |
| ValidateFastq | 0.1.1 |
| OpenJDK | 8 |
| Flask / Gunicorn | 3.1.1 / 23.0.0 |

Compose 用 `micromamba run -n base` 启动 Web 与 worker。仓库只读挂载到 `/opt/bacterial-assembly`；修改流程或 Web 代码后重启服务即可，不必为此重建镜像。

## 本地测试依赖

```bash
python3 -m pip install -r requirements-web.txt
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/smoke.sh
```

Web 部署步骤见 [web_deployment.md](web_deployment.md)。
