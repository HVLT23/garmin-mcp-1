"""Tests for the per-user client cache, contextvar plumbing, and audit log."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from garmin_mcp.config import Settings
from garmin_mcp.registry import CURRENT_USER, LEGACY_USER_ID
from garmin_mcp.server import _LockedGarmin, _PerUserClientCache, make_client_factory


def _settings_with_root(tmp_path: Path) -> Settings:
    return Settings(garmin_tokens_root=tmp_path)


def _stub_load(monkeypatch, raw_factory) -> list[Path]:
    """Patch garmin_mcp.server.load_client to return a fresh raw client per call.

    Returns the list of paths the stub was invoked with (for assertion).
    """
    seen: list[Path] = []

    def fake_load(path):
        seen.append(Path(path))
        return raw_factory(path)

    monkeypatch.setattr("garmin_mcp.server.load_client", fake_load)
    return seen


# ---------- _PerUserClientCache ----------


def test_per_user_cache_returns_distinct_clients(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    seen = _stub_load(monkeypatch, lambda p: MagicMock(name=f"raw-{p.name}"))

    settings = _settings_with_root(tmp_path)
    cache = _PerUserClientCache(settings)

    a = cache.get("alice")
    b = cache.get("bob")
    assert a is not b
    assert isinstance(a, _LockedGarmin) and isinstance(b, _LockedGarmin)
    # Each user's tokens dir was loaded exactly once.
    assert sorted(p.name for p in seen) == ["alice", "bob"]


def test_per_user_cache_lazy_loads_once(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alice").mkdir()
    seen = _stub_load(monkeypatch, lambda p: MagicMock())

    cache = _PerUserClientCache(_settings_with_root(tmp_path))
    a1 = cache.get("alice")
    a2 = cache.get("alice")
    assert a1 is a2
    assert len(seen) == 1


def test_per_user_cache_missing_tokens_raises_authentication_error(
    tmp_path: Path,
) -> None:
    from garmin_mcp.auth import AuthError

    cache = _PerUserClientCache(_settings_with_root(tmp_path))
    with pytest.raises(AuthError) as exc:
        cache.get("never-provisioned")
    assert "never-provisioned" in str(exc.value)
    assert "admin provision" in (exc.value.remediation or "")


def test_legacy_user_falls_back_to_root_when_subdir_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """v1 layout: tokens directly under the root, no `_legacy/` subdir yet."""
    # Root exists (the volume mount) but no `_legacy/` subdir.
    seen = _stub_load(monkeypatch, lambda p: MagicMock())

    cache = _PerUserClientCache(_settings_with_root(tmp_path))
    cache.get(LEGACY_USER_ID)
    assert seen == [tmp_path]  # loaded from root, not <root>/_legacy


def test_legacy_user_prefers_subdir_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    """Once the admin runs the documented `mv`, `_legacy/` wins."""
    legacy_dir = tmp_path / LEGACY_USER_ID
    legacy_dir.mkdir()
    seen = _stub_load(monkeypatch, lambda p: MagicMock())

    cache = _PerUserClientCache(_settings_with_root(tmp_path))
    cache.get(LEGACY_USER_ID)
    assert seen == [legacy_dir]


def test_install_prewarms_cache(tmp_path: Path, monkeypatch) -> None:
    """`install` should make subsequent `get` calls skip the loader."""
    seen = _stub_load(monkeypatch, lambda p: MagicMock())
    cache = _PerUserClientCache(_settings_with_root(tmp_path))

    raw = MagicMock()
    cache.install(LEGACY_USER_ID, raw)
    client = cache.get(LEGACY_USER_ID)
    assert isinstance(client, _LockedGarmin)
    assert client._client is raw
    assert seen == []  # loader never invoked


def test_lock_isolation_between_users(tmp_path: Path, monkeypatch) -> None:
    """A slow first-load for user A must not block requests for user B."""
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()

    enter_alice = threading.Event()
    release_alice = threading.Event()

    def fake_load(path):
        path = Path(path)
        if path.name == "alice":
            enter_alice.set()
            release_alice.wait(timeout=5)
        return MagicMock()

    monkeypatch.setattr("garmin_mcp.server.load_client", fake_load)

    cache = _PerUserClientCache(_settings_with_root(tmp_path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_alice = pool.submit(cache.get, "alice")
        # Wait until alice's loader is mid-flight.
        assert enter_alice.wait(timeout=5)
        # Bob should not be blocked by alice's load.
        t0 = time.monotonic()
        b = pool.submit(cache.get, "bob").result(timeout=2)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5
        assert b is not None
        release_alice.set()
        f_alice.result(timeout=5)


# ---------- factory + ContextVar ----------


def test_factory_reads_current_user_contextvar(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    monkeypatch.setattr(
        "garmin_mcp.server.load_client",
        lambda p: MagicMock(name=f"raw-{Path(p).name}"),
    )

    settings = _settings_with_root(tmp_path)
    factory = make_client_factory(settings)

    token = CURRENT_USER.set("alice")
    try:
        a = factory()
    finally:
        CURRENT_USER.reset(token)

    token = CURRENT_USER.set("bob")
    try:
        b = factory()
    finally:
        CURRENT_USER.reset(token)

    assert a is not b


def test_factory_defaults_to_legacy_when_contextvar_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """Stdio mode has no middleware, so the contextvar is unset."""
    monkeypatch.setattr("garmin_mcp.server.load_client", lambda p: MagicMock())
    settings = _settings_with_root(tmp_path)
    factory = make_client_factory(settings)

    assert CURRENT_USER.get() is None
    client = factory()
    assert isinstance(client, _LockedGarmin)


def test_factory_eager_install_targets_legacy_user(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list = []
    monkeypatch.setattr(
        "garmin_mcp.server.load_client",
        lambda p: seen.append(p) or MagicMock(),
    )
    settings = _settings_with_root(tmp_path)
    eager = MagicMock()
    factory = make_client_factory(settings, eager=eager)

    # _legacy is pre-warmed; calling factory() with the contextvar unset
    # resolves to LEGACY_USER_ID and returns the eager-installed client.
    client = factory()
    assert isinstance(client, _LockedGarmin)
    assert client._client is eager
    assert seen == []  # loader never ran


# ---------- contextvar propagation across anyio.to_thread.run_sync ----------


async def test_contextvar_propagates_across_to_thread(tmp_path: Path) -> None:
    """anyio.to_thread.run_sync must carry the request-scoped user_id into
    the worker thread (Python 3.11+ behaviour). Tools rely on this — if it
    breaks, every tool call would race-condition between users.
    """
    import anyio

    captured: list[str | None] = []

    def in_worker() -> None:
        captured.append(CURRENT_USER.get())

    token = CURRENT_USER.set("alice")
    try:
        await anyio.to_thread.run_sync(in_worker)
    finally:
        CURRENT_USER.reset(token)

    assert captured == ["alice"]


# ---------- cache isolation between users ----------


def test_cache_keys_are_namespaced_per_user(monkeypatch) -> None:
    """Two users calling the same tool with the same args must NOT share
    a cached response.
    """
    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)

    from garmin_mcp import cache
    cache.clear_all()

    counter = {"n": 0}

    @cache.cached(ttl=cache.TTL_WELLNESS)
    def fake_tool(date: str) -> int:
        counter["n"] += 1
        return counter["n"]

    token = CURRENT_USER.set("alice")
    try:
        a1 = fake_tool("2026-01-01")
        a2 = fake_tool("2026-01-01")  # same args, same user → cache hit
    finally:
        CURRENT_USER.reset(token)

    token = CURRENT_USER.set("bob")
    try:
        b1 = fake_tool("2026-01-01")  # same args, DIFFERENT user → cache miss
    finally:
        CURRENT_USER.reset(token)

    assert a1 == a2  # alice cache hit
    assert b1 != a1  # bob got his own value
    assert counter["n"] == 2  # only two distinct invocations


# ---------- audit log ----------


def test_audit_log_emits_one_line_per_call(caplog) -> None:
    from garmin_mcp.tools._helpers import audited

    @audited
    def fake_tool(activity_id: int) -> dict:
        return {"id": activity_id}

    token = CURRENT_USER.set("alice")
    try:
        with caplog.at_level(logging.INFO, logger="garmin_mcp.audit"):
            result = fake_tool(42)
    finally:
        CURRENT_USER.reset(token)

    assert result == {"id": 42}
    audit_records = [r for r in caplog.records if r.name == "garmin_mcp.audit"]
    assert len(audit_records) == 1
    msg = audit_records[0].getMessage()
    # Strict full-match: args must NOT be logged, and nothing else may sneak in.
    # A loose `"42" not in msg` substring check was flaky at ~1.7% — whenever the
    # test ran on a `:42` second, the timestamp `ts=...:42Z` matched and failed.
    assert re.fullmatch(
        r"ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z user=alice tool=fake_tool",
        msg,
    ), f"audit msg shape regression: {msg!r}"


def test_audit_log_uses_legacy_user_when_unset(caplog) -> None:
    from garmin_mcp.tools._helpers import audited

    @audited
    def fake_tool() -> int:
        return 1

    with caplog.at_level(logging.INFO, logger="garmin_mcp.audit"):
        fake_tool()

    rec = next(r for r in caplog.records if r.name == "garmin_mcp.audit")
    assert "user=_legacy" in rec.getMessage()
