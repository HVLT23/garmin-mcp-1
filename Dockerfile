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
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
USER mcp
VOLUME ["/data/tokens"]
EXPOSE 8000
ENTRYPOINT ["garmin-mcp", "serve"]
