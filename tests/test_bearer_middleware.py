"""Tests for the HTTP bearer-token middleware and Authorization header parser.

This is the only authentication boundary in the project, so every code path
through `_parse_bearer_header` and `_bearer_middleware` is exercised here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID, Registry
from garmin_mcp.server import _bearer_middleware, _parse_bearer_header


def _digest(bearer: str) -> str:
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


def _write_registry(path: Path, users: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"users": users}), encoding="utf-8")

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


# ---------- Registry-aware middleware ----------


class _UserCapturingApp:
    """ASGI app that records the CURRENT_USER ContextVar at request time."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, dict[str, Any]]] = []

    async def __call__(self, scope, receive, send) -> None:
        # Read the contextvar inside the downstream call to mirror what a
        # tool function would observe.
        self.calls.append((CURRENT_USER.get(), dict(scope.get("state", {}))))
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


async def test_registry_known_bearer_resolves_user_id(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    bearer = "gmcp_known"
    _write_registry(p, [{"user_id": "alice", "bearer_sha256": _digest(bearer)}])
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)
    sent = await _run(mw, _scope({"authorization": f"Bearer {bearer}"}))

    assert sent[0]["status"] == 200
    assert len(app.calls) == 1
    seen_user, seen_state = app.calls[0]
    assert seen_user == "alice"
    assert seen_state["user_id"] == "alice"


async def test_registry_unknown_bearer_returns_401(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write_registry(p, [{"user_id": "alice", "bearer_sha256": _digest("real")}])
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)
    sent = await _run(mw, _scope({"authorization": "Bearer bogus"}))

    assert sent[0]["status"] == 401
    assert app.calls == []


async def test_registry_empty_falls_back_to_legacy_token(tmp_path: Path) -> None:
    """When the registry file is missing, the legacy single-bearer compare wins."""
    registry = Registry(tmp_path / "nope.json")

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry, legacy_token="legacy-secret")

    sent = await _run(mw, _scope({"authorization": "Bearer legacy-secret"}))
    assert sent[0]["status"] == 200
    assert app.calls[0][0] == LEGACY_USER_ID


async def test_registry_empty_legacy_wrong_token_401(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "nope.json")

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry, legacy_token="legacy-secret")
    sent = await _run(mw, _scope({"authorization": "Bearer wrong"}))
    assert sent[0]["status"] == 401
    assert app.calls == []


async def test_populated_registry_rejects_legacy_bearer(tmp_path: Path) -> None:
    """Once a non-empty registry exists, the legacy bearer is no longer accepted."""
    p = tmp_path / "registry.json"
    _write_registry(p, [{"user_id": "alice", "bearer_sha256": _digest("real")}])
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry, legacy_token="legacy-secret")
    sent = await _run(mw, _scope({"authorization": "Bearer legacy-secret"}))
    assert sent[0]["status"] == 401
    assert app.calls == []


async def test_no_legacy_token_and_empty_registry_rejects(tmp_path: Path) -> None:
    """If there's no registry AND no legacy token, every request 401s."""
    registry = Registry(tmp_path / "nope.json")

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry, legacy_token=None)
    sent = await _run(mw, _scope({"authorization": "Bearer anything"}))
    assert sent[0]["status"] == 401


async def test_context_var_is_reset_after_request(tmp_path: Path) -> None:
    """Leaking CURRENT_USER between requests would be a cross-tenant bug."""
    p = tmp_path / "registry.json"
    _write_registry(p, [{"user_id": "alice", "bearer_sha256": _digest("a")}])
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)

    assert CURRENT_USER.get() is None
    await _run(mw, _scope({"authorization": "Bearer a"}))
    assert CURRENT_USER.get() is None  # reset after request


# ---------- corrupt registry: 503, not uvicorn 500 ----------


async def test_corrupt_registry_returns_503_with_actionable_body(tmp_path: Path) -> None:
    """A malformed registry must NOT bubble up as a 500 — return 503 with
    a JSON body the operator can act on."""
    p = tmp_path / "registry.json"
    p.write_text("{not json", encoding="utf-8")
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)
    sent = await _run(mw, _scope({"authorization": "Bearer anything"}))

    assert sent[0]["status"] == 503
    assert app.calls == []
    body = sent[1]["body"]
    assert b"registry_corrupt" in body
    assert str(p).encode() in body  # remediation path is included
    # Retry-After header is set so callers back off.
    headers = dict(sent[0]["headers"])
    assert headers.get(b"retry-after") == b"30"


async def test_corrupt_registry_logs_once_per_distinct_error(
    tmp_path: Path, caplog
) -> None:
    """Spamming one log line per request would flood the audit trail.
    Dedup so a single corrupt-registry state produces one log line."""
    import logging as _logging

    p = tmp_path / "registry.json"
    p.write_text("{not json", encoding="utf-8")
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)

    with caplog.at_level(_logging.ERROR, logger="garmin_mcp.server"):
        for _ in range(5):
            await _run(mw, _scope({"authorization": "Bearer anything"}))

    corrupt_records = [
        r for r in caplog.records
        if r.name == "garmin_mcp.server" and "registry corrupt" in r.getMessage()
    ]
    assert len(corrupt_records) == 1, (
        f"expected one log line for the same corrupt-registry state, got "
        f"{len(corrupt_records)}"
    )


async def test_recovery_from_corrupt_registry(tmp_path: Path) -> None:
    """Once the operator fixes the registry, requests must succeed again
    on the next mtime change (no server restart required)."""
    p = tmp_path / "registry.json"
    p.write_text("{not json", encoding="utf-8")
    registry = Registry(p)

    app = _UserCapturingApp()
    mw = _bearer_middleware(app, registry)
    sent = await _run(mw, _scope({"authorization": "Bearer rescue"}))
    assert sent[0]["status"] == 503

    # Operator fixes the file.
    _write_registry(p, [{"user_id": "alice", "bearer_sha256": _digest("rescue")}])
    sent = await _run(mw, _scope({"authorization": "Bearer rescue"}))
    assert sent[0]["status"] == 200
    assert app.calls[0][0] == "alice"
