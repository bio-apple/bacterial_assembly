FROM anaconda/miniconda:26.9.4
ENV CONDA_PLUGINS_AUTO_ACCEPT_TOS=true \
    PYTHONDONTWRITEBYTECODE=1
RUN conda install --yes --name base --channel conda-forge --channel bioconda \
        shovill=1.4.2 \
        skesa=2.5.1 \
    && conda clean --all --yes
RUN useradd --create-home --uid 1000 --user-group tooluser
USER tooluser
WORKDIR /work
ENTRYPOINT ["shovill"]
