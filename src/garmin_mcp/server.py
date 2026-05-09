"""FastMCP server construction, transport selection, and bearer-token auth."""

from __future__ import annotations

import hmac
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP

from garmin_mcp.auth import AuthError, load_client, verify
from garmin_mcp.config import Settings, load_settings
from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID, Registry, load_registry
from garmin_mcp.tools import register_all

logger = logging.getLogger(__name__)


ClientFactory = Callable[[], Garmin]


class _LockedGarmin:
    """Thin proxy that serialises every attribute access into a `Garmin` instance.

    `garminconnect` is built on `garth`, which uses a single `requests.Session`
    per client and is not documented as thread-safe. FastMCP's HTTP transport
    runs sync tools concurrently via `anyio.to_thread.run_sync`, so without
    serialisation two parallel tool calls could race during a token refresh
    or share an in-flight HTTP connection. We wrap the client and acquire a
    per-instance lock around every attribute read — both method calls and
    property reads (`client.profile`, `client.last_activity`, etc.) — so a
    future tool that reaches for a property doesn't silently bypass the
    lock. Tool code is unchanged: it still does `client_factory().get_X(...)`.

    **Type-checker contract.** This class does NOT inherit from `Garmin` and
    is not a `Garmin` subtype. The factory hands it back via `cast(Garmin, ...)`
    so call sites stay annotated as `Garmin`, but the cast is a deliberate
    lie — duck typing carries it. Do **not** add `isinstance(client, Garmin)`
    checks anywhere downstream: the proxy will silently take the wrong branch.
    Treat the factory return as opaque and call methods/attributes on it.
    """

    __slots__ = ("_client", "_lock")

    def __init__(self, client: Garmin) -> None:
        self._client = client
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        # Lock the attribute *read* too: garth/garminconnect surface lazy
        # properties (e.g. `client.profile`) that issue HTTP requests on
        # first access, so unlocked reads could race the same way unlocked
        # method calls would. For plain in-memory attributes this is a
        # negligible extra lock acquisition.
        with self._lock:
            attr = getattr(self._client, name)
        if not callable(attr):
            return attr
        lock = self._lock

        def locked(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return attr(*args, **kwargs)

        return locked


def _stderr_logging() -> None:
    """Stdio MCP requires a clean stdout — push all logs to stderr."""
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class _PerUserClientCache:
    """Lazy-load and cache one `_LockedGarmin` per `user_id`.

    The first request for a user acquires that user's lock and runs the
    SSO/token-load flow exactly once; subsequent requests return the
    cached proxy. Per-user locks (rather than one global lock) mean a
    slow first-load for user A doesn't block requests for user B.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, Garmin] = {}
        # One creation lock per user_id, plus a registry lock so we don't
        # race when allocating those per-user locks.
        self._user_locks: dict[str, threading.Lock] = {}
        self._user_locks_lock = threading.Lock()

    def _lock_for(self, user_id: str) -> threading.Lock:
        with self._user_locks_lock:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[user_id] = lock
            return lock

    def install(self, user_id: str, eager: Garmin) -> None:
        """Pre-warm the cache for a user (called for legacy mode at startup)."""
        self._clients[user_id] = cast(Garmin, _LockedGarmin(eager))

    def _resolve_tokens_dir(self, user_id: str) -> Path:
        """Find the tokens directory for a user, with a v1-layout fallback.

        Multi-tenant layout: `<root>/<user_id>/`. As a one-time migration
        helper, the `_legacy` user falls back to the root itself when
        `<root>/_legacy/` doesn't yet exist — that's the v1 single-tenant
        layout where Garmin tokens lived directly under
        `GARMIN_TOKENS_PATH`. Once an admin runs the documented
        `mv /data/tokens/garmin_tokens.json /data/tokens/_legacy/`, the
        primary path takes over.
        """
        primary = self._settings.tokens_dir_for(user_id)
        if primary.exists():
            return primary
        if user_id == LEGACY_USER_ID:
            root = self._settings.garmin_tokens_root
            if root.exists():
                return root
        return primary

    def get(self, user_id: str) -> Garmin:
        client = self._clients.get(user_id)
        if client is not None:
            return client
        lock = self._lock_for(user_id)
        with lock:
            client = self._clients.get(user_id)
            if client is None:
                tokens_dir = self._resolve_tokens_dir(user_id)
                if not tokens_dir.exists():
                    raise AuthError(
                        f"no tokens for user_id={user_id!r} at {tokens_dir}",
                        remediation=(
                            "admin must run "
                            f"`garmin-mcp admin provision --user-id {user_id}`"
                        ),
                    )
                raw = load_client(tokens_dir)
                client = cast(Garmin, _LockedGarmin(raw))
                self._clients[user_id] = client
            return client


def make_client_factory(
    settings: Settings,
    *,
    eager: Garmin | None = None,
    cache: _PerUserClientCache | None = None,
) -> ClientFactory:
    """Build a factory that returns the *current* user's Garmin client.

    The factory reads the active user_id from the `CURRENT_USER` ContextVar
    set by the bearer middleware on each authenticated request. In
    legacy/single-bearer mode that user_id is `LEGACY_USER_ID`. Stdio
    mode (no middleware) defaults to `LEGACY_USER_ID` too.

    If `eager` is provided, the legacy user's client is pre-warmed so the
    existing single-tenant traffic doesn't pay first-call latency.
    """
    cache = cache or _PerUserClientCache(settings)
    if eager is not None:
        cache.install(LEGACY_USER_ID, eager)

    def _factory() -> Garmin:
        user_id = CURRENT_USER.get() or LEGACY_USER_ID
        return cache.get(user_id)

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


async def _send_401(send: Any) -> None:
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


def _legacy_bearer_match(candidate: str | None, expected_token: str) -> bool:
    """Constant-time bearer comparison for the legacy single-bearer fallback.

    Missing / short candidates are right-padded with NUL bytes to the
    expected token's length so the no-header / wrong-length / wrong-content
    paths all run the same compare and don't leak timing information.
    Bearer tokens come from env vars which can't contain NUL bytes, so
    NUL-padding can never collide with a real expected value.
    """
    expected_bytes = expected_token.encode("utf-8")
    candidate_bytes = (candidate or "").encode("utf-8").ljust(len(expected_bytes), b"\x00")
    return hmac.compare_digest(candidate_bytes, expected_bytes)


def _bearer_middleware(
    app: Any,
    auth: str | Registry,
    *,
    legacy_token: str | None = None,
):
    """Reject HTTP requests without a recognised `Authorization: Bearer <token>`.

    `auth` is either:
      - a `Registry` instance — bearer is sha256'd and looked up; on miss
        we fall through to legacy_token (if registry is empty) or 401.
      - a plain string — preserves the v1 single-bearer behaviour for
        unit tests and for the case where no registry path is configured.

    On success we set the `CURRENT_USER` ContextVar so the per-user
    client factory can pick up the user_id without threading it through
    every tool signature, and stash it in `scope["state"]` for any
    downstream ASGI middleware that prefers reading from there.
    """
    is_registry = isinstance(auth, Registry)

    async def asgi(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        candidate = _parse_bearer_header(headers.get("authorization", ""))

        user_id: str | None = None
        if is_registry:
            registry = cast(Registry, auth)
            if registry.is_empty():
                # Legacy fallback: registry is unpopulated, so the v1
                # single-bearer comparison takes over and traffic is
                # tagged as the `_legacy` user. Once a non-empty registry
                # is dropped on disk, this branch auto-disables.
                if legacy_token and _legacy_bearer_match(candidate, legacy_token):
                    user_id = LEGACY_USER_ID
            else:
                if candidate:
                    user_id = registry.lookup(candidate)
        else:
            # Pure single-bearer mode — used by tests and when the registry
            # path is configured but absent and no legacy token is set.
            if _legacy_bearer_match(candidate, cast(str, auth)):
                user_id = LEGACY_USER_ID

        if user_id is None:
            await _send_401(send)
            return

        scope.setdefault("state", {})["user_id"] = user_id
        token = CURRENT_USER.set(user_id)
        try:
            await app(scope, receive, send)
        finally:
            CURRENT_USER.reset(token)

    return asgi


def _healthz_wrapper(app: Any) -> Any:
    """Intercept GET /healthz and respond 200 without auth.

    Mounted *outside* the bearer middleware so Fly.io's HTTP healthcheck
    (and any other liveness probe) can hit it anonymously. Everything else
    passes through to the wrapped app unchanged.
    """

    async def asgi(scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") in ("/healthz", "/healthz/")
            and scope.get("method", "GET").upper() == "GET"
        ):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"status":"ok"}',
            })
            return
        await app(scope, receive, send)

    return asgi


def _eager_legacy_path(settings: Settings) -> Path:
    """Where to look for the legacy single-tenant tokens during startup.

    Prefer `<root>/_legacy/` (post-migration); fall back to `<root>/`
    itself (pre-migration v1 layout) so the owner's traffic keeps working
    in the deploy window before the SSH `mv` runs.
    """
    primary = settings.tokens_dir_for(LEGACY_USER_ID)
    if primary.exists():
        return primary
    return settings.garmin_tokens_root


def serve(settings: Settings | None = None) -> None:
    """Entry point used by the CLI's `serve` subcommand."""
    _stderr_logging()
    settings = settings or load_settings()

    registry = load_registry(settings.garmin_registry_path)
    legacy_active = registry.is_empty()

    if (
        settings.mcp_transport == "http"
        and legacy_active
        and not settings.mcp_bearer_token
        and not settings.mcp_allow_unauthenticated
    ):
        logger.error(
            "HTTP transport requires either a populated registry at %s "
            "or MCP_BEARER_TOKEN (legacy mode). "
            "Set MCP_ALLOW_UNAUTHENTICATED=1 to override.",
            registry.path,
        )
        raise SystemExit(2)

    # Eagerly load the legacy user's tokens (when legacy mode is active) so
    # the existing single-tenant traffic doesn't pay first-call latency.
    # Multi-tenant users are loaded lazily on first request by the
    # per-user client cache.
    eager_client: Garmin | None = None
    if legacy_active:
        legacy_tokens = _eager_legacy_path(settings)
        try:
            eager_client = load_client(legacy_tokens)
            verify(eager_client)
            logger.info("Garmin auth OK (legacy user, tokens at %s)", legacy_tokens)
        except AuthError as e:
            logger.error("Garmin auth failed: %s — %s", e, e.remediation)
            # Still start the server: tools will return structured auth errors.
            # This keeps stdio clients responsive instead of crashing on startup.
    else:
        logger.info(
            "Multi-tenant mode: %d user(s) registered at %s",
            len(registry.user_ids()),
            registry.path,
        )

    # `verify(eager_client)` above ran on the raw Garmin instance — fine, it's
    # a one-shot before any concurrency exists. `build_server` then hands the
    # client to `make_client_factory`, which wraps it in `_LockedGarmin` *before*
    # `register_all` exposes any tool. There's no window in which a tool could
    # observe the unwrapped client.
    mcp, settings = build_server(settings, eager_client=eager_client)

    if settings.mcp_transport == "stdio":
        mcp.run(transport="stdio")
        return

    # HTTP transport — wrap with bearer auth and run via uvicorn.
    import uvicorn

    app = mcp.streamable_http_app()
    if not legacy_active or settings.mcp_bearer_token:
        app = _bearer_middleware(
            app, registry, legacy_token=settings.mcp_bearer_token
        )
        if legacy_active:
            logger.info(
                "Bearer-token auth enabled (legacy single-bearer mode active "
                "until %s is populated)",
                registry.path,
            )
        else:
            logger.info("Bearer-token auth enabled (multi-tenant registry)")
    else:
        logger.warning(
            "HTTP transport without registry or MCP_BEARER_TOKEN "
            "(MCP_ALLOW_UNAUTHENTICATED=1) — server is unauthenticated"
        )
    # /healthz is wrapped *outside* the bearer middleware so liveness probes
    # (Fly.io HTTP checks etc.) can hit it without credentials.
    app = _healthz_wrapper(app)

    config = uvicorn.Config(app, host=settings.mcp_host, port=settings.mcp_port, log_level="info")
    uvicorn.Server(config).run()
