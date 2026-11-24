FROM mambaorg/micromamba:1.5.10

RUN micromamba install --yes --name base \
    --channel conda-forge \
    --channel bioconda \
    python=3.10 \
    fastp=0.23.4 \
    spades=3.15.5 \
    shovill=1.4.2 \
    skesa=2.5.1 \
    megahit=1.2.9 \
    quast=5.2.0 \
    flask=3.1.1 \
    gunicorn=23.0.0 \
    && micromamba create --yes --name unicycler \
        --channel conda-forge \
        --channel bioconda \
        python=3.10 \
        unicycler=0.5.1 \
        spades=4.0.0 \
    && micromamba clean --all --yes

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
