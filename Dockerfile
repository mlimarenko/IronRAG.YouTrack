FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    PATH=/app/connector/.venv/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=8088 \
    LOG_LEVEL=info \
    ROUTING_CONFIG_PATH=/app/routing.yaml \
    STATE_DB_PATH=/var/lib/ironrag-connector/state.sqlite

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 connector \
    && useradd --system --uid 10001 --gid connector --home-dir /nonexistent connector \
    && mkdir -p /var/lib/ironrag-connector \
    && chown connector:connector /var/lib/ironrag-connector

RUN pip install uv==0.8.0

WORKDIR /app/connector

COPY framework /app/connectortemplate
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-editable --no-dev

USER connector:connector
VOLUME ["/var/lib/ironrag-connector"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

CMD ["youtrack-connector"]
