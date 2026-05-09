"""FastMCP server construction, transport selection, and bearer-token auth."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.auth import AuthError, load_client, verify
from garmin_mcp.config import Settings, load_settings
from garmin_mcp.tools import register_all

logger = logging.getLogger(__name__)


ClientFactory = Callable[[], Garmin]


def _stderr_logging() -> None:
    """Stdio MCP requires a clean stdout — push all logs to stderr."""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def make_client_factory(settings: Settings) -> ClientFactory:
    """Build a memoised lazy factory that returns a logged-in Garmin client."""
    cached: dict[str, Garmin] = {}

    def _factory() -> Garmin:
        if "client" in cached:
            return cached["client"]
        client = load_client(settings.garmin_tokens_path)
        cached["client"] = client
        return client

    return _factory


def build_server(settings: Settings | None = None) -> tuple[FastMCP, Settings]:
    """Construct the FastMCP server with all tools registered."""
    settings = settings or load_settings()
    mcp = FastMCP(
        "garmin-mcp",
        instructions=(
            "Read-only access to the user's Garmin Connect data — activities, "
            "wellness (sleep/HRV/body battery/stress/steps), and training "
            "metrics — for analysing training sessions in the context of "
            "daily-life signals."
        ),
        host=settings.mcp_host,
        port=settings.mcp_port,
    )
    factory = make_client_factory(settings)
    register_all(mcp, factory)
    return mcp, settings


def _bearer_middleware(app, expected_token: str):
    """Reject HTTP requests without `Authorization: Bearer <token>`.

    Implemented as a raw ASGI wrapper so we don't need to import starlette
    middleware machinery.
    """

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        expected = f"Bearer {expected_token}"
        if auth != expected:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="garmin-mcp"')],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized"}',
            })
            return
        await app(scope, receive, send)

    return asgi


def serve(settings: Settings | None = None) -> None:
    """Entry point used by the CLI's `serve` subcommand."""
    _stderr_logging()
    mcp, settings = build_server(settings)

    # Validate auth eagerly so the operator gets a clear error before any
    # MCP traffic arrives.
    try:
        name = verify(load_client(settings.garmin_tokens_path))
        logger.info("Garmin auth OK for %s", name)
    except AuthError as e:
        logger.error("Garmin auth failed: %s — %s", e, e.remediation)
        # Still start the server: tools will return structured auth errors.
        # This keeps stdio clients responsive instead of crashing on startup.

    if settings.mcp_transport == "stdio":
        mcp.run(transport="stdio")
        return

    # HTTP transport — optionally wrap with bearer auth and run via uvicorn.
    import uvicorn

    app = mcp.streamable_http_app()
    if settings.mcp_bearer_token:
        app = _bearer_middleware(app, settings.mcp_bearer_token)
        logger.info("Bearer-token auth enabled for HTTP transport")
    else:
        logger.warning("HTTP transport without MCP_BEARER_TOKEN — server is unauthenticated")

    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")
    uvicorn.Server(config).run()
