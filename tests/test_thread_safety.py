"""Tests for the thread-safety proxy and the /healthz endpoint.

The proxy (`_LockedGarmin`) is the M2 fix: HTTP-mode FastMCP runs sync tools
concurrently via `anyio.to_thread.run_sync`, and `garth.requests.Session` is
not documented as thread-safe. We assert that no two calls into the wrapped
client overlap, even when the wrapper is invoked from a thread pool.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock

from garmin_mcp.server import _healthz_wrapper, _LockedGarmin, make_client_factory

# ---------- _LockedGarmin: serialisation under concurrency ----------


class _OverlapDetector:
    """Mock body that tracks concurrent entry. `max_concurrent` records the
    highest number of threads observed inside the call simultaneously. Any
    value > 1 means the lock failed.
    """

    def __init__(self, hold_seconds: float = 0.02) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_concurrent = 0
        self.calls = 0
        self.hold = hold_seconds

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        with self.lock:
            self.active += 1
            self.calls += 1
            if self.active > self.max_concurrent:
                self.max_concurrent = self.active
        try:
            time.sleep(self.hold)
            return self.calls
        finally:
            with self.lock:
                self.active -= 1


def test_locked_garmin_serialises_concurrent_calls() -> None:
    detector = _OverlapDetector(hold_seconds=0.02)
    raw = MagicMock()
    raw.get_activities = detector
    proxy = _LockedGarmin(raw)

    n_workers = 8
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(proxy.get_activities, 0, 20) for _ in range(n_workers)]
        results = [f.result(timeout=5) for f in futures]

    assert detector.calls == n_workers
    assert len(results) == n_workers
    # The whole point of the proxy: never more than one in-flight call.
    assert detector.max_concurrent == 1, (
        f"expected serialised access, observed {detector.max_concurrent} concurrent calls"
    )


def test_locked_garmin_serialises_across_different_methods() -> None:
    """The lock is per-client, not per-method — a get_activities call must
    block a parallel get_sleep_data on the same client."""
    detector = _OverlapDetector(hold_seconds=0.02)
    raw = MagicMock()
    raw.get_activities = detector
    raw.get_sleep_data = detector  # same callable so we share the counter
    proxy = _LockedGarmin(raw)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for _ in range(4):
            futures.append(pool.submit(proxy.get_activities, 0, 20))
            futures.append(pool.submit(proxy.get_sleep_data, "2026-05-09"))
        for f in futures:
            f.result(timeout=5)

    assert detector.max_concurrent == 1


def test_locked_garmin_forwards_return_value_and_args() -> None:
    raw = MagicMock()
    raw.get_activity.return_value = {"activityId": 42}
    proxy = _LockedGarmin(raw)

    result = proxy.get_activity("42")

    assert result == {"activityId": 42}
    raw.get_activity.assert_called_once_with("42")


def test_locked_garmin_propagates_exceptions() -> None:
    raw = MagicMock()
    raw.get_activity.side_effect = RuntimeError("boom")
    proxy = _LockedGarmin(raw)

    try:
        proxy.get_activity("42")
    except RuntimeError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("expected RuntimeError to propagate through the proxy")


def test_locked_garmin_passes_through_non_callable_attributes() -> None:
    raw = MagicMock()
    raw.display_name = "Test User"  # imagined string attribute
    proxy = _LockedGarmin(raw)
    assert proxy.display_name == "Test User"


def test_factory_returns_locked_proxy_for_eager_client() -> None:
    """`make_client_factory(eager=...)` must wrap the eager client so callers
    that bypass `load_client` still get serialised access."""
    from garmin_mcp.config import Settings

    raw = MagicMock()
    settings = Settings()
    factory = make_client_factory(settings, eager=raw)
    client = factory()
    # Calling through the factory hits the proxy's __getattr__, not the raw
    # MagicMock directly. Easiest invariant to assert is the wrapped type.
    assert isinstance(client, _LockedGarmin)
    # And the underlying client is preserved.
    assert client._client is raw


# ---------- /healthz endpoint ----------


class _DownstreamApp:
    """Records every call so we can assert the healthz path doesn't leak."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 418,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"teapot"})


async def _drive(app, scope) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    await app(scope, receive, send)
    return sent


def _http_scope(path: str, method: str = "GET", headers: dict[str, str] | None = None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }


async def test_healthz_returns_200_without_auth() -> None:
    downstream = _DownstreamApp()
    app = _healthz_wrapper(downstream)

    sent = await _drive(app, _http_scope("/healthz"))

    assert downstream.calls == []  # bearer middleware was never reached
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b'{"status":"ok"}'


async def test_healthz_does_not_intercept_other_paths() -> None:
    downstream = _DownstreamApp()
    app = _healthz_wrapper(downstream)

    sent = await _drive(app, _http_scope("/mcp"))

    assert len(downstream.calls) == 1
    assert downstream.calls[0]["path"] == "/mcp"
    assert sent[0]["status"] == 418  # downstream reply, not 200


async def test_healthz_only_responds_to_get() -> None:
    """A POST to /healthz should fall through to the downstream app rather
    than being incorrectly handled here."""
    downstream = _DownstreamApp()
    app = _healthz_wrapper(downstream)

    sent = await _drive(app, _http_scope("/healthz", method="POST"))

    assert len(downstream.calls) == 1
    assert sent[0]["status"] == 418


async def test_healthz_bypasses_bearer_middleware() -> None:
    """End-to-end: stack the healthz wrapper *outside* bearer middleware so
    /healthz is reachable without an Authorization header.
    """
    from garmin_mcp.server import _bearer_middleware

    downstream = _DownstreamApp()
    protected = _bearer_middleware(downstream, "secret")
    app = _healthz_wrapper(protected)

    sent = await _drive(app, _http_scope("/healthz"))

    assert downstream.calls == []
    assert sent[0]["status"] == 200


async def test_non_healthz_still_requires_bearer_when_stacked() -> None:
    """Verify the wrapper composition doesn't accidentally bypass auth for
    non-healthz paths."""
    from garmin_mcp.server import _bearer_middleware

    downstream = _DownstreamApp()
    protected = _bearer_middleware(downstream, "secret")
    app = _healthz_wrapper(protected)

    sent = await _drive(app, _http_scope("/mcp"))  # no Authorization header

    assert downstream.calls == []
    assert sent[0]["status"] == 401
