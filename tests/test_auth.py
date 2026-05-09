"""Tests for token lifecycle and AuthError plumbing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from garmin_mcp import auth


def test_load_client_missing_tokens_raises(tmp_path: Path) -> None:
    with pytest.raises(auth.AuthError) as exc:
        auth.load_client(tmp_path / "missing")
    assert "No tokens found" in str(exc.value)
    assert "auth login" in (exc.value.remediation or "")


def test_load_client_calls_login_with_tokenstore(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    fake = MagicMock()
    fake.return_value.login.return_value = (None, None)

    with patch("garmin_mcp.auth.Garmin", fake):
        client = auth.load_client(tokens)

    fake.assert_called_once_with()
    fake.return_value.login.assert_called_once_with(tokenstore=str(tokens))
    assert client is fake.return_value


def test_load_client_translates_auth_error(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    fake = MagicMock()
    fake.return_value.login.side_effect = GarminConnectAuthenticationError("401")

    with patch("garmin_mcp.auth.Garmin", fake), pytest.raises(auth.AuthError) as exc:
        auth.load_client(tokens)

    assert "Stored tokens were rejected" in str(exc.value)
    assert "re-run" in (exc.value.remediation or "")


def test_load_client_translates_connection_error(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    fake = MagicMock()
    fake.return_value.login.side_effect = GarminConnectConnectionError("network down")

    with patch("garmin_mcp.auth.Garmin", fake), pytest.raises(auth.AuthError) as exc:
        auth.load_client(tokens)

    assert "connection error" in str(exc.value).lower()
    # Default remediation kicks in when the connection error path doesn't
    # supply a more specific one.
    assert "auth login" in (exc.value.remediation or "")


def test_login_interactive_translates_auth_error(tmp_path: Path) -> None:
    fake = MagicMock()
    fake.return_value.login.side_effect = GarminConnectAuthenticationError("nope")

    with patch("garmin_mcp.auth.Garmin", fake), pytest.raises(auth.AuthError) as exc:
        auth.login_interactive(
            email="x@y.z",
            password="pw",
            tokens_path=tmp_path / "tokens",
            mfa_prompt=lambda: "000000",
        )
    assert "rejected credentials" in str(exc.value)


def test_make_mfa_prompt_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("GARMIN_MFA", "123456")
    assert auth.make_mfa_prompt()() == "123456"


def test_make_mfa_prompt_raises_without_tty(monkeypatch) -> None:
    monkeypatch.delenv("GARMIN_MFA", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    prompt = auth.make_mfa_prompt()
    with pytest.raises(auth.AuthError) as exc:
        prompt()
    assert "no TTY" in str(exc.value)
    assert "GARMIN_MFA" in (exc.value.remediation or "")


def test_verify_returns_full_name() -> None:
    fake_client = MagicMock()
    fake_client.get_full_name.return_value = "Test User"
    assert auth.verify(fake_client) == "Test User"


def test_verify_no_name_raises() -> None:
    fake_client = MagicMock()
    fake_client.get_full_name.return_value = None
    with pytest.raises(auth.AuthError):
        auth.verify(fake_client)


def test_harden_tokens_chmods_dir_and_files(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir(mode=0o755)
    inner = tokens / "oauth1_token.json"
    inner.write_text("secret", encoding="utf-8")
    inner.chmod(0o644)

    auth._harden_tokens(tokens)

    assert (tokens.stat().st_mode & 0o777) == 0o700
    assert (inner.stat().st_mode & 0o777) == 0o600
