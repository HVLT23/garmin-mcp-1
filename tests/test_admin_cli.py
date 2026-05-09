"""Tests for `garmin-mcp admin` CLI: provision / revoke / list."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from garmin_mcp.cli import main


def _digest(bearer: str) -> str:
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch):
    """Point Settings at a tmp tokens root and registry path via env vars."""
    monkeypatch.setenv("GARMIN_TOKENS_PATH", str(tmp_path / "tokens"))
    monkeypatch.setenv("GARMIN_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return {
        "tokens_root": tmp_path / "tokens",
        "registry_path": tmp_path / "registry.json",
    }


def test_admin_provision_creates_registry_entry_and_prints_bearer(
    isolated_settings, monkeypatch
) -> None:
    """End-to-end: provision creates tokens dir, registry entry, and prints
    a bearer that hashes to the registered sha256."""

    def fake_login(email, password, tokens_path, mfa_prompt):
        # Simulate garminconnect creating the tokens dir.
        tokens_path.mkdir(parents=True, exist_ok=True)
        (tokens_path / "garmin_tokens.json").write_text("{}")
        client = MagicMock()
        client.get_full_name.return_value = "Alice Test"
        return client

    monkeypatch.setattr("garmin_mcp.cli.login_interactive", fake_login)
    monkeypatch.setattr("garmin_mcp.cli.verify", lambda c: c.get_full_name())

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "admin", "provision",
            "--user-id", "alice",
            "--email", "alice@example.com",
            "--password", "hunter2",
        ],
    )

    assert result.exit_code == 0, result.stderr

    # The bearer is the only thing on stdout (everything else goes to stderr).
    bearer = result.stdout.strip()
    assert bearer.startswith("gmcp_")
    assert len(bearer) > len("gmcp_")

    # Registry was written with the matching sha256.
    reg_path = isolated_settings["registry_path"]
    assert reg_path.exists()
    doc = json.loads(reg_path.read_text())
    assert doc == {"users": [{"user_id": "alice", "bearer_sha256": _digest(bearer)}]}

    # Tokens dir exists for alice.
    assert (isolated_settings["tokens_root"] / "alice").is_dir()


def test_admin_provision_rejects_duplicate_user(isolated_settings, monkeypatch) -> None:
    reg_path = isolated_settings["registry_path"]
    reg_path.write_text(json.dumps({
        "users": [{"user_id": "alice", "bearer_sha256": _digest("x")}]
    }))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "provision", "--user-id", "alice",
         "--email", "a@b.c", "--password", "p"],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_admin_provision_rejects_bad_user_id(isolated_settings) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "provision", "--user-id", "../escape",
         "--email", "a@b.c", "--password", "p"],
    )
    assert result.exit_code != 0


def test_admin_revoke_removes_entry(isolated_settings, monkeypatch) -> None:
    reg_path = isolated_settings["registry_path"]
    reg_path.write_text(json.dumps({
        "users": [
            {"user_id": "alice", "bearer_sha256": _digest("a")},
            {"user_id": "bob", "bearer_sha256": _digest("b")},
        ]
    }))

    runner = CliRunner()
    result = runner.invoke(main, ["admin", "revoke", "--user-id", "alice"])
    assert result.exit_code == 0, result.output

    doc = json.loads(reg_path.read_text())
    assert doc == {"users": [{"user_id": "bob", "bearer_sha256": _digest("b")}]}


def test_admin_revoke_rejects_path_traversal_user_id(
    isolated_settings, monkeypatch, tmp_path
) -> None:
    """A hand-edited registry containing a malicious user_id must not let
    `revoke --purge-tokens` rm-rf its way out of the tokens root."""
    sentinel = tmp_path / "sentinel-must-survive"
    sentinel.write_text("alive")

    runner = CliRunner()
    for bad in ("../etc", "../", "..", "alice/..", ".hidden", "foo/bar"):
        result = runner.invoke(
            main,
            ["admin", "revoke", "--user-id", bad, "--purge-tokens"],
        )
        assert result.exit_code != 0, f"expected rejection for {bad!r}"
    # Sentinel still exists — no path-traversal rmtree happened.
    assert sentinel.exists()
    assert sentinel.read_text() == "alive"


def test_admin_revoke_unknown_user_fails(isolated_settings) -> None:
    isolated_settings["registry_path"].write_text(json.dumps({"users": []}))
    runner = CliRunner()
    result = runner.invoke(main, ["admin", "revoke", "--user-id", "nobody"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_admin_revoke_purge_tokens(isolated_settings) -> None:
    reg_path = isolated_settings["registry_path"]
    reg_path.write_text(json.dumps({
        "users": [{"user_id": "alice", "bearer_sha256": _digest("a")}]
    }))
    tokens_root = isolated_settings["tokens_root"]
    (tokens_root / "alice").mkdir(parents=True)
    (tokens_root / "alice" / "garmin_tokens.json").write_text("{}")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "revoke", "--user-id", "alice", "--purge-tokens"],
    )
    assert result.exit_code == 0
    assert not (tokens_root / "alice").exists()


def test_admin_list_prints_users_and_first_8_of_sha256(isolated_settings) -> None:
    reg_path = isolated_settings["registry_path"]
    digest_a = _digest("a")
    reg_path.write_text(json.dumps({
        "users": [
            {"user_id": "alice", "bearer_sha256": digest_a},
        ]
    }))
    (isolated_settings["tokens_root"] / "alice").mkdir(parents=True)

    runner = CliRunner()
    result = runner.invoke(main, ["admin", "list"])
    assert result.exit_code == 0, result.output

    # All `admin list` output goes to stderr — stdout is reserved for
    # secrets (the one-time bearer in `provision`). Verifying this also
    # locks in the M3 reviewer fix.
    assert result.stdout == ""
    out = result.stderr
    assert digest_a[:8] in out
    assert digest_a not in out  # full hash NOT printed
    assert "alice" in out


def test_admin_list_empty(isolated_settings) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["admin", "list"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "empty" in result.stderr


def test_provision_partial_failure_surfaces_remediation(
    isolated_settings, monkeypatch
) -> None:
    """If `write_registry_atomic` fails after tokens are written, the operator
    must see explicit remediation pointing at the orphaned tokens dir."""

    def fake_login(email, password, tokens_path, mfa_prompt):
        tokens_path.mkdir(parents=True, exist_ok=True)
        (tokens_path / "garmin_tokens.json").write_text("{}")
        client = MagicMock()
        client.get_full_name.return_value = "Alice"
        return client

    def boom(path, entries):
        raise OSError("disk full")

    monkeypatch.setattr("garmin_mcp.cli.login_interactive", fake_login)
    monkeypatch.setattr("garmin_mcp.cli.verify", lambda c: c.get_full_name())
    monkeypatch.setattr("garmin_mcp.cli.write_registry_atomic", boom)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "provision", "--user-id", "alice",
         "--email", "a@b.c", "--password", "p"],
    )
    assert result.exit_code != 0
    assert "PARTIAL PROVISION" in result.stderr
    # Both remediation paths are mentioned by the message.
    assert "re-run" in result.stderr
    assert "rm -rf" in result.stderr
    # Tokens dir was created but no registry written.
    assert (isolated_settings["tokens_root"] / "alice").is_dir()
    assert not isolated_settings["registry_path"].exists()


def test_bearer_never_persisted_in_plaintext(isolated_settings, monkeypatch) -> None:
    """The provision flow must never write the bearer to disk anywhere."""

    def fake_login(email, password, tokens_path, mfa_prompt):
        tokens_path.mkdir(parents=True, exist_ok=True)
        client = MagicMock()
        client.get_full_name.return_value = "Alice"
        return client

    monkeypatch.setattr("garmin_mcp.cli.login_interactive", fake_login)
    monkeypatch.setattr("garmin_mcp.cli.verify", lambda c: c.get_full_name())

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "provision", "--user-id", "alice",
         "--email", "a@b.c", "--password", "p"],
    )
    assert result.exit_code == 0
    bearer = result.stdout.strip()

    # Walk every file under tmp_path and confirm the bearer doesn't appear.
    root = isolated_settings["registry_path"].parent
    for p in root.rglob("*"):
        if p.is_file():
            content = p.read_bytes()
            assert bearer.encode() not in content, f"bearer leaked into {p}"
