"""Tests for the bearer-token registry: parse, validation, hot reload."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from garmin_mcp.registry import (
    Registry,
    RegistryError,
    load_registry,
    write_registry_atomic,
)


def _digest(bearer: str) -> str:
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_missing_file_yields_empty_registry(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "registry.json")
    assert reg.is_empty()
    assert reg.lookup("anything") is None
    assert reg.user_ids() == []


def test_empty_users_array_yields_empty_registry(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": []})
    reg = Registry(p)
    assert reg.is_empty()


def test_lookup_resolves_known_bearer(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    bearer = "gmcp_topsecret"
    _write(p, {"users": [{"user_id": "alice", "bearer_sha256": _digest(bearer)}]})
    reg = Registry(p)
    assert reg.lookup(bearer) == "alice"
    assert reg.lookup("not-a-bearer") is None
    assert not reg.is_empty()
    assert reg.user_ids() == ["alice"]


def test_lookup_empty_bearer_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": [{"user_id": "alice", "bearer_sha256": _digest("x")}]})
    reg = Registry(p)
    assert reg.lookup("") is None


def test_invalid_top_level_raises(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    p.write_text("[]", encoding="utf-8")
    reg = Registry(p)
    with pytest.raises(RegistryError, match="top-level must be a JSON object"):
        reg.lookup("anything")


def test_users_must_be_list(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": "nope"})
    reg = Registry(p)
    with pytest.raises(RegistryError, match=r"users.*must be a list"):
        reg.lookup("anything")


def test_user_id_must_be_non_empty_string(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": [{"user_id": "", "bearer_sha256": _digest("x")}]})
    reg = Registry(p)
    with pytest.raises(RegistryError, match="user_id"):
        reg.lookup("anything")


def test_bearer_sha256_must_be_hex_64(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": [{"user_id": "alice", "bearer_sha256": "tooshort"}]})
    reg = Registry(p)
    with pytest.raises(RegistryError, match="bearer_sha256"):
        reg.lookup("anything")


def test_unsafe_user_id_rejected_at_parse_time(tmp_path: Path) -> None:
    """Defence-in-depth: a malicious user_id that slipped past the admin
    CLI (or was hand-edited into the registry) must be rejected here too,
    so the per-user client lookup never resolves a path-traversal value.
    """
    p = tmp_path / "registry.json"
    for bad in ("../etc", "alice/..", "..", ".hidden", "with\\backslash"):
        _write(p, {"users": [{"user_id": bad, "bearer_sha256": _digest("x")}]})
        reg = Registry(p)
        with pytest.raises(RegistryError, match="unsafe"):
            reg.lookup("anything")


def test_duplicate_user_id_rejected(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": [
        {"user_id": "alice", "bearer_sha256": _digest("a")},
        {"user_id": "alice", "bearer_sha256": _digest("b")},
    ]})
    reg = Registry(p)
    with pytest.raises(RegistryError, match="duplicate user_id"):
        reg.lookup("anything")


def test_duplicate_bearer_rejected(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    same = _digest("shared")
    _write(p, {"users": [
        {"user_id": "alice", "bearer_sha256": same},
        {"user_id": "bob", "bearer_sha256": same},
    ]})
    reg = Registry(p)
    with pytest.raises(RegistryError, match="duplicate bearer_sha256"):
        reg.lookup("anything")


def test_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    p.write_text("{not json", encoding="utf-8")
    reg = Registry(p)
    with pytest.raises(RegistryError, match="not valid JSON"):
        reg.lookup("anything")


def test_hot_reload_on_mtime_change(tmp_path: Path) -> None:
    """Adding a user without restarting must be picked up on next lookup.

    The registry uses nanosecond-precision mtime so consecutive writes
    within the same second are still detected — no sleep required.
    """
    p = tmp_path / "registry.json"
    _write(p, {"users": [{"user_id": "alice", "bearer_sha256": _digest("a")}]})
    reg = Registry(p)
    assert reg.lookup("a") == "alice"
    assert reg.lookup("b") is None

    _write(p, {"users": [
        {"user_id": "alice", "bearer_sha256": _digest("a")},
        {"user_id": "bob", "bearer_sha256": _digest("b")},
    ]})
    assert reg.lookup("b") == "bob"
    assert reg.lookup("a") == "alice"


def test_hot_reload_picks_up_file_appearing(tmp_path: Path) -> None:
    """Registry can transition from empty (legacy mode) to populated."""
    p = tmp_path / "registry.json"
    reg = Registry(p)
    assert reg.is_empty()

    _write(p, {"users": [{"user_id": "alice", "bearer_sha256": _digest("a")}]})
    assert reg.lookup("a") == "alice"
    assert not reg.is_empty()


def test_resolve_path_explicit_overrides_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GARMIN_REGISTRY_PATH", "/tmp/from-env.json")
    reg = load_registry(tmp_path / "explicit.json")
    assert reg.path == tmp_path / "explicit.json"


def test_resolve_path_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("GARMIN_REGISTRY_PATH", "/tmp/from-env.json")
    reg = load_registry(None)
    assert str(reg.path) == "/tmp/from-env.json"


def test_resolve_path_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("GARMIN_REGISTRY_PATH", raising=False)
    reg = load_registry(None)
    assert reg.path.name == "registry.json"


def test_default_registry_lives_inside_default_tokens_root() -> None:
    """Regression guard for the C1 release-blocker: the registry must sit
    under the default tokens root so it survives Fly machine restarts on
    the same persistent volume. If a future config change re-introduces a
    rootfs-only path (e.g. /data/registry.json), every restart would wipe
    the registry and silently break multi-tenant auth.
    """
    from garmin_mcp.config import Settings
    from garmin_mcp.registry import DEFAULT_REGISTRY_PATH

    settings = Settings()
    # In CI the default tokens root resolves to ~/.config/garmin-mcp/tokens,
    # not /data/tokens — so we can't compare to a literal path. Instead we
    # assert the *Fly-deployment* default holds: registry sits inside the
    # canonical tokens volume.
    assert str(DEFAULT_REGISTRY_PATH).startswith("/data/tokens/"), (
        f"DEFAULT_REGISTRY_PATH={DEFAULT_REGISTRY_PATH} must live under "
        "/data/tokens/ so it shares the persistent volume with the "
        "tokens themselves"
    )
    # And: anyone overriding tokens root via env should see the registry
    # default still resolve under /data/tokens (we don't try to derive
    # the registry path from settings.garmin_tokens_root — the registry
    # default is independent — but we lock it on the canonical path).
    _ = settings  # silence unused; kept to make the relationship explicit


def test_write_registry_atomic_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    bearer_a = "gmcp_a"
    write_registry_atomic(p, [
        {"user_id": "alice", "bearer_sha256": _digest(bearer_a)},
    ])
    assert p.exists()
    reg = Registry(p)
    assert reg.lookup(bearer_a) == "alice"


def test_write_registry_atomic_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deeper" / "registry.json"
    write_registry_atomic(p, [])
    assert p.exists()
    reg = Registry(p)
    assert reg.is_empty()


def test_entries_are_sorted_by_user_id(tmp_path: Path) -> None:
    p = tmp_path / "registry.json"
    _write(p, {"users": [
        {"user_id": "zelda", "bearer_sha256": _digest("z")},
        {"user_id": "alice", "bearer_sha256": _digest("a")},
        {"user_id": "mallory", "bearer_sha256": _digest("m")},
    ]})
    reg = Registry(p)
    assert reg.user_ids() == ["alice", "mallory", "zelda"]
