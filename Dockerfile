# ValidateFastq 等依赖以 linux-64 为主。在 arm64 宿主机上请使用
# docker build --platform linux/amd64。
FROM anaconda/miniconda:26.9.4

# Compose 用 `micromamba run -n base` 启动。生信组装/评估在工具侧车镜像中运行（ADR 0010）。
ENV CONDA_PLUGINS_AUTO_ACCEPT_TOS=true \
    MAMBA_ROOT_PREFIX=/opt/miniconda3 \
    PYTHONDONTWRITEBYTECODE=1

RUN conda install --yes --name base \
        --channel conda-forge \
        --channel bioconda \
        python=3.10 \
        micromamba \
        flask=3.1.1 \
        gunicorn=23.0.0 \
        openjdk=8 \
        biopet-validatefastq=0.1.1 \
        pip \
    && pip install --no-cache-dir \
        docker==7.1.0 \
        'psycopg[binary]==3.2.3' \
        redis==5.2.1 \
        rq==2.1.0 \
    && micromamba clean --all --yes \
    && conda clean --all --yes

WORKDIR /work
