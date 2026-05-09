"""Tests for the HTTP bearer-token middleware and Authorization header parser.

This is the only authentication boundary in the project, so every code path
through `_parse_bearer_header` and `_bearer_middleware` is exercised here.
"""

from __future__ import annotations

from typing import Any

from garmin_mcp.server import _bearer_middleware, _parse_bearer_header

# ---------- _parse_bearer_header unit tests ----------

def test_parse_bearer_uppercase_scheme() -> None:
    assert _parse_bearer_header("Bearer abc") == "abc"


def test_parse_bearer_lowercase_scheme() -> None:
    # RFC 7235: scheme tokens are case-insensitive.
    assert _parse_bearer_header("bearer abc") == "abc"


def test_parse_bearer_mixed_case_scheme() -> None:
    assert _parse_bearer_header("BeArEr abc") == "abc"


def test_parse_non_bearer_scheme_rejected() -> None:
    assert _parse_bearer_header("Basic dXNlcjpwYXNz") is None


def test_parse_empty_header() -> None:
    assert _parse_bearer_header("") is None


def test_parse_no_credential_after_scheme() -> None:
    # Just "Bearer" with no space and credential — partition gives ("", "", "")
    # and the scheme check fails because partition returns "Bearer" as the head.
    assert _parse_bearer_header("Bearer") is None


def test_parse_empty_credential_after_space() -> None:
    # "Bearer " with trailing space → empty credential string.
    assert _parse_bearer_header("Bearer ") == ""


# ---------- _bearer_middleware integration tests ----------

def _scope(headers: dict[str, str] | None = None, scope_type: str = "http") -> dict[str, Any]:
    return {
        "type": scope_type,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }


class _StubApp:
    """Records every downstream call so tests can assert pass-through vs. blocked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})


async def _run(middleware, scope) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    await middleware(scope, receive, send)
    return sent


async def test_missing_auth_header_returns_401() -> None:
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope())
    assert app.calls == []  # downstream never invoked
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


async def test_correct_token_passes_through_uppercase() -> None:
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope({"authorization": "Bearer secret-token"}))
    assert len(app.calls) == 1
    assert sent[0]["status"] == 200


async def test_correct_token_passes_through_lowercase_scheme() -> None:
    """RFC 7235 says the scheme token is case-insensitive."""
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope({"authorization": "bearer secret-token"}))
    assert len(app.calls) == 1
    assert sent[0]["status"] == 200


async def test_wrong_token_returns_401() -> None:
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope({"authorization": "Bearer wrong-token"}))
    assert app.calls == []
    assert sent[0]["status"] == 401


async def test_empty_credential_after_bearer_returns_401() -> None:
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope({"authorization": "Bearer "}))
    assert app.calls == []
    assert sent[0]["status"] == 401


async def test_wrong_scheme_returns_401() -> None:
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")
    sent = await _run(mw, _scope({"authorization": "Basic c2VjcmV0LXRva2Vu"}))
    assert app.calls == []
    assert sent[0]["status"] == 401


async def test_short_candidate_is_padded_not_accepted() -> None:
    """A short candidate gets NUL-padded to expected length but still fails."""
    app = _StubApp()
    mw = _bearer_middleware(app, "the-very-long-secret")
    sent = await _run(mw, _scope({"authorization": "Bearer the"}))
    assert app.calls == []
    assert sent[0]["status"] == 401


async def test_lifespan_scope_passes_through_unauthenticated() -> None:
    """ASGI lifespan / websocket scopes don't carry HTTP auth — must pass through."""
    app = _StubApp()
    mw = _bearer_middleware(app, "secret-token")

    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    await mw({"type": "lifespan", "headers": []}, receive, send)
    # Lifespan is forwarded straight to the app — no 401 short-circuit.
    assert len(app.calls) == 1
    assert app.calls[0]["type"] == "lifespan"
