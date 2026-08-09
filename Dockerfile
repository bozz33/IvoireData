# syntax=docker/dockerfile:1
# IvoireData — image locale de production (API + scheduler + moteur de collecte)
# Multi-stage : construction d'un venv, puis copie dans un runtime slim.

ARG PY_VERSION=3.12
ARG PUID=1000
ARG PGID=1000

FROM python:${PY_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install --no-cache-dir '.[dev]'

FROM python:${PY_VERSION}-slim AS runtime

ARG PUID
ARG PGID

LABEL org.opencontainers.image.title="IvoireData" \
      org.opencontainers.image.description="Moteur local de collecte et livraison de données Côte d'Ivoire" \
      org.opencontainers.image.source="https://github.com/bozz33/IvoireData" \
      org.opencontainers.image.version="0.7.0" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    IVOIREDATA_DATA_DIR=/app/data_lake \
    IVOIREDATA_STATE_DIR=/app/.ivoiredata/state

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid ${PGID} ivoire \
    && useradd --system --uid ${PUID} --gid ivoire --create-home --home-dir /home/ivoire --shell /bin/bash ivoire

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src ./src
COPY registry ./registry
COPY configs ./configs
COPY scripts ./scripts
COPY VERSION README.md ./

RUN mkdir -p /app/data_lake /app/.ivoiredata/state \
    && chown -R ivoire:ivoire /app

USER ivoire

EXPOSE 8000

CMD ["uvicorn", "ivoiredata.api:app", "--host", "0.0.0.0", "--port", "8000"]
