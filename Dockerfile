# syntax=docker/dockerfile:1
# IvoireData — image locale de production (API + scheduler + corpus/tokenizer)
# Multi-stage : on construit un venv dans le builder, on le recopie dans un runtime slim.
# Builder et runtime partagent la même image de base pour que les extensions C
# (pandas, pyarrow, pyreadr, tokenizers) restent compatibles.

ARG PY_VERSION=3.12
# UID/GID de l'utilisateur applicatif. Par défaut 1000:1000 pour matcher un user Linux
# classique ; surchargeable via `docker compose build --build-arg PUID=1001 PGID=1001`.
ARG PUID=1000
ARG PGID=1000

# ---------- builder ----------
FROM python:${PY_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential uniquement au cas où une wheel manquerait ; le builder est jeté.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
# [training] apporte tokenizers ; [dev] apporte pytest pour lancer les tests dans l'image.
RUN pip install --upgrade pip \
    && pip install --no-cache-dir '.[training,dev]'


# ---------- runtime ----------
FROM python:${PY_VERSION}-slim AS runtime

# Les ARG globaux (déclarés avant les FROM) doivent être re-déclarés dans chaque stage
# qui les utilise, sinon ils valent la chaîne vide ici.
ARG PUID
ARG PGID

LABEL org.opencontainers.image.title="IvoireData" \
      org.opencontainers.image.description="Moteur local de données Côte d'Ivoire et usine de corpus" \
      org.opencontainers.image.source="https://github.com/bozz33/IvoireData" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    IVOIREDATA_DATA_DIR=/app/data_lake \
    IVOIREDATA_STATE_DIR=/app/.ivoiredata/state

# curl pour le HEALTHCHECK de l'API.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid ${PGID} ivoire \
    && useradd --system --uid ${PUID} --gid ivoire --create-home --home-dir /home/ivoire --shell /bin/bash ivoire

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Code + ressources nécessaires à l'exécution (registry/configs lus par l'engine).
COPY src ./src
COPY registry ./registry
COPY configs ./configs
COPY scripts ./scripts
COPY VERSION README.md ./

# Volumes persistants montés par docker-compose (data_lake, .ivoiredata, corpora, tokenizer).
RUN mkdir -p /app/data_lake /app/.ivoiredata/state /app/corpora /app/tokenizer \
    && chown -R ivoire:ivoire /app

USER ivoire

EXPOSE 8000

# L'entrypoint reste l'API ; le compose surcharge la commande pour scheduler/sync.
CMD ["uvicorn", "ivoiredata.api:app", "--host", "0.0.0.0", "--port", "8000"]
