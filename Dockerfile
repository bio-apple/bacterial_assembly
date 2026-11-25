# 这些 Bioconda 包只有 linux-64 构建。在 arm64 宿主机上请使用
# docker build --platform linux/amd64。
FROM anaconda/miniconda:26.9.4

# Compose 用 `micromamba run -n base` 启动。与 Miniconda 共用前缀，
# 这样 base 与独立的 unicycler 环境都能被 micromamba 找到。
ENV CONDA_PLUGINS_AUTO_ACCEPT_TOS=true \
    MAMBA_ROOT_PREFIX=/opt/miniconda3 \
    PYTHONDONTWRITEBYTECODE=1

RUN conda install --yes --name base \
        --channel conda-forge \
        --channel bioconda \
        python=3.10 \
        micromamba \
        fastp=0.23.4 \
        spades=3.15.5 \
        shovill=1.4.2 \
        skesa=2.5.1 \
        megahit=1.2.9 \
        quast=5.2.0 \
        flask=3.1.1 \
        gunicorn=23.0.0 \
        openjdk=8 \
        biopet-validatefastq=0.1.1 \
    && micromamba create --yes --name unicycler \
        --channel conda-forge \
        --channel bioconda \
        python=3.10 \
        unicycler=0.5.1 \
        spades=4.0.0 \
    && micromamba clean --all --yes \
    && conda clean --all --yes

WORKDIR /work
