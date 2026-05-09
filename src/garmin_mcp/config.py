"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_tokens_path() -> Path:
    """Resolve the default tokens directory.

    Order:
      1. $GARMIN_TOKENS_PATH (handled by the Settings field, not here)
      2. $XDG_CONFIG_HOME/garmin-mcp/tokens
      3. ~/.config/garmin-mcp/tokens
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "garmin-mcp" / "tokens"


class Settings(BaseSettings):
    """Process-wide settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    garmin_email: str | None = None
    garmin_password: str | None = None
    garmin_mfa: str | None = None
    garmin_tokens_path: Path = Field(default_factory=default_tokens_path)

    mcp_transport: Literal["stdio", "http"] = "stdio"
    # Bind to localhost by default — running HTTP transport on 0.0.0.0 with
    # no bearer token would expose the user's Garmin data to the entire LAN.
    # The Docker image overrides this to 0.0.0.0 inside the container.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_bearer_token: str | None = None
    # Refuse to start HTTP transport without a bearer token unless this is
    # explicitly set to "1". Stdio is unaffected.
    mcp_allow_unauthenticated: bool = False

    garmin_mcp_no_cache: bool = False


def load_settings() -> Settings:
    """Build a fresh Settings instance (re-reads env each call)."""
    return Settings()
