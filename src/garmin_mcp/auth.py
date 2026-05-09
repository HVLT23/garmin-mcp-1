"""Token lifecycle and Garmin client construction."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

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
    path.parent.mkdir(parents=True, exist_ok=True)


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

    return client


def make_mfa_prompt(env_var: str = "GARMIN_MFA") -> Callable[[], str]:
    """Return a callable that supplies the MFA code.

    Uses the env var if present (useful for headless first-time auth);
    otherwise prompts on stderr/stdin so it never pollutes stdio MCP traffic.
    """

    def _prompt() -> str:
        env = os.environ.get(env_var)
        if env:
            return env
        # Print prompt to stderr to avoid corrupting any stdio JSON-RPC stream.
        print("Garmin MFA code: ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip()

    return _prompt


def verify(client: Garmin) -> str:
    """Cheap call to confirm the session works. Returns the user's full name."""
    name = client.get_full_name()
    if not name:
        raise AuthError("Authenticated but profile lookup failed")
    return name
