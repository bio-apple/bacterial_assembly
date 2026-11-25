# Build with an explicit --platform or rely on host detection (ADR 0019).
# Supported: linux/amd64 | linux/arm64. Example: ./docker/tools/build.sh
# Conda packages are unpinned; image tag is :latest (ADR 0010).
FROM anaconda/miniconda
ENV CONDA_PLUGINS_AUTO_ACCEPT_TOS=true \
    PYTHONDONTWRITEBYTECODE=1
RUN conda install --yes --name base --channel conda-forge --channel bioconda \
        shovill \
        skesa \
    && conda clean --all --yes
RUN useradd --create-home --uid 1000 --user-group tooluser
USER tooluser
WORKDIR /work
ENTRYPOINT ["shovill"]
