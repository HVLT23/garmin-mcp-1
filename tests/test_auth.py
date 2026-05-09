"""Tests for token lifecycle and AuthError plumbing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def test_make_mfa_prompt_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("GARMIN_MFA", "123456")
    assert auth.make_mfa_prompt()() == "123456"


def test_verify_returns_full_name() -> None:
    fake_client = MagicMock()
    fake_client.get_full_name.return_value = "Test User"
    assert auth.verify(fake_client) == "Test User"


def test_verify_no_name_raises() -> None:
    fake_client = MagicMock()
    fake_client.get_full_name.return_value = None
    with pytest.raises(auth.AuthError):
        auth.verify(fake_client)
