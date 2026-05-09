FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

FROM python:3.14-slim
WORKDIR /app
RUN useradd --create-home --uid 1000 mcp \
    && mkdir -p /data/tokens \
    && chown -R mcp:mcp /data /app
COPY --from=builder --chown=mcp:mcp /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    GARMIN_TOKENS_PATH=/data/tokens \
    GARMIN_REGISTRY_PATH=/data/tokens/registry.json \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
USER mcp
VOLUME ["/data/tokens"]
EXPOSE 8000
# Container-level healthcheck against the unauthenticated /healthz route.
# Fly.io has its own HTTP healthcheck wired up in fly.toml; this is for
# `docker run` / Compose users. Python avoids pulling curl into the image.
# Reads MCP_PORT at probe time so a Compose user overriding the listener
# port doesn't end up with a healthcheck pinned to 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('MCP_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else 1)" \
  || exit 1
ENTRYPOINT ["garmin-mcp", "serve"]
