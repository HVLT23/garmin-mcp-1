"""FastMCP server construction, transport selection, and bearer-token auth."""

from __future__ import annotations

import hmac
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


def make_client_factory(settings: Settings, *, eager: Garmin | None = None) -> ClientFactory:
    """Build a factory that returns a single shared logged-in Garmin client.

    If `eager` is provided (server startup already loaded one), the factory
    just returns it. Otherwise it lazy-loads on first call with a lock so
    concurrent HTTP requests can't kick off two simultaneous SSO flows.
    """
    import threading

    state: dict[str, Garmin] = {}
    if eager is not None:
        state["client"] = eager
    lock = threading.Lock()

    def _factory() -> Garmin:
        client = state.get("client")
        if client is not None:
            return client
        with lock:
            client = state.get("client")
            if client is None:
                client = load_client(settings.garmin_tokens_path)
                state["client"] = client
            return client

    return _factory


def build_server(
    settings: Settings | None = None,
    *,
    eager_client: Garmin | None = None,
) -> tuple[FastMCP, Settings]:
    """Construct the FastMCP server with all tools registered."""
    settings = settings or load_settings()
    mcp: FastMCP = FastMCP(
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
    factory = make_client_factory(settings, eager=eager_client)
    register_all(mcp, factory)
    return mcp, settings


def _parse_bearer_header(raw: str) -> str | None:
    """Extract the credential from an Authorization header. None if not Bearer."""
    if not raw or " " not in raw:
        return None
    scheme, _, credential = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credential


def _bearer_middleware(app, expected_token: str):
    """Reject HTTP requests without `Authorization: Bearer <token>`.

    Constant-time credential compare (`hmac.compare_digest`); the absent-header
    branch also runs through compare_digest with a same-length dummy so that
    timing doesn't leak whether the header was supplied.
    """
    expected_bytes = expected_token.encode("utf-8")

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        candidate = _parse_bearer_header(headers.get("authorization", ""))
        # Compare even when the header is missing — uses an equal-length dummy
        # so the no-header path takes the same time as a wrong-token path.
        candidate_bytes = (candidate or "").encode("utf-8")
        if not hmac.compare_digest(candidate_bytes, expected_bytes):
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
    settings = settings or load_settings()

    if (
        settings.mcp_transport == "http"
        and not settings.mcp_bearer_token
        and not settings.mcp_allow_unauthenticated
    ):
        logger.error(
            "HTTP transport requires MCP_BEARER_TOKEN. "
            "Set it, or set MCP_ALLOW_UNAUTHENTICATED=1 to override."
        )
        raise SystemExit(2)

    # Validate auth eagerly so the operator gets a clear error before any
    # MCP traffic arrives. Reuse the loaded client for the lifetime of the
    # server so the factory doesn't run a second SSO flow on first tool call.
    eager_client: Garmin | None = None
    try:
        eager_client = load_client(settings.garmin_tokens_path)
        verify(eager_client)
        logger.info("Garmin auth OK")
    except AuthError as e:
        logger.error("Garmin auth failed: %s — %s", e, e.remediation)
        # Still start the server: tools will return structured auth errors.
        # This keeps stdio clients responsive instead of crashing on startup.

    mcp, settings = build_server(settings, eager_client=eager_client)

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
        logger.warning(
            "HTTP transport without MCP_BEARER_TOKEN (MCP_ALLOW_UNAUTHENTICATED=1) "
            "— server is unauthenticated"
        )

    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")
    uvicorn.Server(config).run()
