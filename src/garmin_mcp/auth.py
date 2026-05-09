"""Token lifecycle and Garmin client construction."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import click
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Raised when tokens are missing or refused by Garmin."""

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.remediation = remediation or "run `garmin-mcp auth login`"


def _ensure_parent(path: Path) -> None:
    """Create the tokens directory with 0o700 perms (or tighten if it exists)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is masked by umask, so set it explicitly afterwards.
    try:
        path.parent.chmod(0o700)
    except OSError as e:
        logger.warning("could not chmod %s: %s", path.parent, e)


def _harden_tokens(tokens_path: Path) -> None:
    """Walk the tokens dir and chmod everything to user-only access.

    `garth` is opaque about exactly which files it writes; rather than guessing
    the layout, we tighten everything under the directory to 0o600 (files)
    and 0o700 (subdirs).
    """
    if not tokens_path.exists():
        return
    try:
        if tokens_path.is_dir():
            tokens_path.chmod(0o700)
            for child in tokens_path.rglob("*"):
                child.chmod(0o700 if child.is_dir() else 0o600)
        else:
            tokens_path.chmod(0o600)
    except OSError as e:
        logger.warning("could not harden token permissions under %s: %s", tokens_path, e)


def login_interactive(
    email: str,
    password: str,
    tokens_path: Path,
    mfa_prompt: Callable[[], str],
) -> Garmin:
    """Perform a fresh SSO login and persist tokens.

    Garmin.login(tokenstore=path) handles writing both OAuth1 and OAuth2 tokens
    to the directory after a successful credentialed login.
    """
    _ensure_parent(tokens_path)
    client = Garmin(email=email, password=password, prompt_mfa=mfa_prompt)
    try:
        client.login(tokenstore=str(tokens_path))
    except GarminConnectAuthenticationError as e:
        raise AuthError(f"Garmin rejected credentials: {e}") from e
    except GarminConnectConnectionError as e:
        raise AuthError(f"Garmin connection error during login: {e}") from e
    _harden_tokens(tokens_path)
    return client


def load_client(tokens_path: Path) -> Garmin:
    """Load a Garmin client from persisted tokens.

    Raises AuthError if tokens are missing, malformed, or expired beyond
    auto-refresh.
    """
    if not tokens_path.exists():
        raise AuthError(
            f"No tokens found at {tokens_path}",
            remediation="run `garmin-mcp auth login`",
        )

    client = Garmin()
    try:
        client.login(tokenstore=str(tokens_path))
    except GarminConnectAuthenticationError as e:
        raise AuthError(
            f"Stored tokens were rejected: {e}",
            remediation="re-run `garmin-mcp auth login`",
        ) from e
    except GarminConnectConnectionError as e:
        raise AuthError(f"Garmin connection error: {e}") from e
    except Exception as e:
        raise AuthError(f"Failed to restore session from tokens: {e}") from e

    # garth may have refreshed and re-written tokens; tighten perms again.
    _harden_tokens(tokens_path)
    return client


def make_mfa_prompt(env_var: str = "GARMIN_MFA") -> Callable[[], str]:
    """Return a callable that supplies the MFA code.

    Uses the env var if present (useful for headless first-time auth);
    otherwise prompts on stderr/stdin so it never pollutes stdio MCP traffic.
    Raises AuthError immediately if no env var and no TTY — better than
    blocking forever on an unreachable stdin.
    """

    def _prompt() -> str:
        env = os.environ.get(env_var)
        if env:
            return env
        if not sys.stdin.isatty():
            raise AuthError(
                "MFA required but no TTY available and GARMIN_MFA env var not set",
                remediation=f"set {env_var} in your environment and re-run",
            )
        click.echo("Garmin MFA code: ", nl=False, err=True)
        return sys.stdin.readline().strip()

    return _prompt


def verify(client: Garmin) -> str:
    """Cheap call to confirm the session works. Returns the user's full name."""
    name = client.get_full_name()
    if not name:
        raise AuthError("Authenticated but profile lookup failed")
    return name
