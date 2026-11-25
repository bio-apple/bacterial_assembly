# Build with an explicit --platform or rely on host detection (ADR 0019).
# Supported: linux/amd64 | linux/arm64. Example: ./docker/tools/build.sh
FROM anaconda/miniconda:26.9.4
ENV CONDA_PLUGINS_AUTO_ACCEPT_TOS=true \
    PYTHONDONTWRITEBYTECODE=1
RUN conda install --yes --name base --channel conda-forge --channel bioconda \
        spades=3.15.5 \
    && conda clean --all --yes
RUN useradd --create-home --uid 1000 --user-group tooluser
USER tooluser
WORKDIR /work
ENTRYPOINT ["spades.py"]
