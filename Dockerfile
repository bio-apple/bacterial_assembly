# Build via ./docker/build-app.sh (platform: --platform > PLATFORM > host; ADR 0019).
# Supported: linux/amd64 | linux/arm64. Do not hardcode platform in Compose.
FROM anaconda/miniconda:26.9.4

# Compose uses `micromamba run -n base`. Bioinformatics runs in tool sidecars (ADR 0010).
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
